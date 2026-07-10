"""SwitchBot 防水温湿度計の読取値から「シャワー使用」を判定するモジュール。

判定アルゴリズム:
  - 直近 BASELINE_WINDOW 分の湿度の中央値を平常値とする
  - 平常値 + SPIKE_THRESHOLD_PCT 以上の急上昇が SPIKE_DETECT_MINUTES 以内に発生
    → "shower_start" イベント発火
  - shower_start から COOLDOWN_MINUTES 以内は再発火しない
  - 湿度が平常値+10%以下に戻ったら "shower_end" イベント発火
"""
from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger("bath_humidity")

BASELINE_WINDOW_MIN = 30          # 平常湿度を取る過去分数
SPIKE_THRESHOLD_PCT = 25          # +25%以上で急上昇とみなす
SPIKE_DETECT_MINUTES = 3          # 何分以内の上昇を「急」とみなすか
COOLDOWN_MINUTES = 30             # shower_start の再発火を抑制
END_DROP_PCT = 10                 # +10%以下に戻ったらシャワー終了
ABNORMAL_TEMP_C = 35.0            # 浴室空気がこれ以上で異常通知


@dataclass
class _Sample:
    ts: datetime
    humidity: int
    temperature: float


class BathHumidityDetector:
    """温湿度計の読取値を毎回 feed() して、状態遷移を内部管理する。"""

    def __init__(self):
        self._history: deque[_Sample] = deque(maxlen=300)
        self._shower_active = False
        self._last_shower_start: datetime | None = None
        self._abnormal_temp_warned: datetime | None = None

    def feed(self, humidity_pct: int, temperature_c: float, ts: datetime | None = None
             ) -> list[tuple[str, dict]]:
        """新しい読取値を投入して、新規イベント（shower_start / shower_end /
        abnormal_temp 等）のリストを返す。
        """
        now = ts or datetime.now()
        self._history.append(_Sample(now, humidity_pct, temperature_c))
        events: list[tuple[str, dict]] = []

        # 過去30分の湿度から平常値を算出（読取値が3件以上あること）
        baseline_cutoff = now - timedelta(minutes=BASELINE_WINDOW_MIN)
        baseline_samples = [s.humidity for s in self._history if s.ts >= baseline_cutoff]
        if len(baseline_samples) < 3:
            return events
        baseline = statistics.median(baseline_samples)

        # 急上昇検出（数分以内の湿度差）
        spike_cutoff = now - timedelta(minutes=SPIKE_DETECT_MINUTES)
        recent = [s for s in self._history if s.ts >= spike_cutoff]
        if recent:
            spike_min = min(s.humidity for s in recent)
            spike_max = max(s.humidity for s in recent)
            spike_delta = spike_max - spike_min

            cooldown_ok = (
                self._last_shower_start is None
                or (now - self._last_shower_start).total_seconds() / 60 >= COOLDOWN_MINUTES
            )
            if (not self._shower_active and cooldown_ok
                    and humidity_pct >= baseline + SPIKE_THRESHOLD_PCT
                    and spike_delta >= SPIKE_THRESHOLD_PCT):
                self._shower_active = True
                self._last_shower_start = now
                events.append(("shower_start", {
                    "humidity": humidity_pct,
                    "baseline": int(baseline),
                    "delta": int(spike_delta),
                    "temperature": temperature_c,
                }))
                log.info("シャワー開始検知: 湿度 %d%% (平常%d%% + %d)",
                         humidity_pct, int(baseline), int(spike_delta))

        # シャワー終了検出（湿度が平常値+10%以下に戻った）
        if self._shower_active and humidity_pct <= baseline + END_DROP_PCT:
            self._shower_active = False
            duration = ((now - self._last_shower_start).total_seconds() / 60
                        if self._last_shower_start else 0)
            events.append(("shower_end", {
                "humidity": humidity_pct,
                "baseline": int(baseline),
                "duration_minutes": round(duration, 1),
            }))
            log.info("シャワー終了検知（持続 %.1f分）", duration)

        # 異常高温検知（1時間に1回まで）
        if temperature_c >= ABNORMAL_TEMP_C:
            if (self._abnormal_temp_warned is None
                    or (now - self._abnormal_temp_warned).total_seconds() >= 3600):
                self._abnormal_temp_warned = now
                events.append(("abnormal_temp", {
                    "temperature": temperature_c,
                    "humidity": humidity_pct,
                }))

        return events

    @property
    def is_shower_active(self) -> bool:
        return self._shower_active
