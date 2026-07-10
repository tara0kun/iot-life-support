"""お風呂利用イベント検知 + LINE 質問（学習データ収集）。

仕組み:
  1. 湿度の急上昇 / 浴室ドア状態変化 / 脱衣所モーション の総合判定で
     「誰かが浴室を使っている」候補を検知
  2. 検知したら DB に bath_classifications レコードを作成（confirmed_person_id=NULL）
  3. LINE で全家族に Quick Reply「誰がお風呂に入っていますか？」を送信
  4. 家族の回答を bath_classifications.confirmed_person_id に保存（学習データ）
  5. 後日、同じシグナルパターンが充分に学習されたら自動判定（Phase 2、未実装）

クールダウン: 30分以内に既に検知済みなら新規発火しない（誤連発防止）。
"""
from __future__ import annotations

import logging
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable

log = logging.getLogger("bath_detector")

# 検知パラメータ（実環境で調整）
HUMIDITY_RISE_THRESHOLD = 8.0       # 湿度上昇 +8% 以上で「使用中」候補
HUMIDITY_HISTORY_MINUTES = 5         # ベースライン算出に使う直近秒数
COOLDOWN_MINUTES = 30                # 検知後は30分間 新規検知抑制
MIN_PEAK_DURATION_SECONDS = 60       # ピーク維持が短すぎるノイズ除外


@dataclass
class _Sample:
    timestamp: datetime
    humidity: float
    temperature: float


@dataclass
class BathCandidate:
    detected_at: datetime
    door_was_closed: bool
    humidity_baseline: float
    humidity_peak: float
    humidity_delta: float
    temperature_delta: float
    motion_count: int
    active_person_id: int | None


class BathDetector:
    """センサ信号を統合してお風呂利用候補を検知し、コールバックを呼ぶ。

    monitor.py から:
      - feed_humidity(humidity, temperature, ts) を温湿度計のたびに呼ぶ
      - feed_door(is_open, ts) を風呂ドアT110のたびに呼ぶ
      - feed_motion(ts) を脱衣所T100のたびに呼ぶ
      - get_active_person() を別途渡しておき、検知時に参照

    検知時 on_candidate(candidate) を await で呼ぶ。
    """

    def __init__(
        self,
        db_path: str,
        on_candidate: Callable[[BathCandidate, int], Awaitable[None]] | None = None,
        get_active_person_fn: Callable[[], int | None] | None = None,
    ):
        self._db_path = db_path
        self._on_candidate = on_candidate
        self._get_active_person = get_active_person_fn or (lambda: None)
        self._humidity_history: deque[_Sample] = deque(maxlen=600)  # 最大1時間分（10秒x600）
        self._last_door_closed_at: datetime | None = None
        self._is_door_open: bool = True  # デフォルト開（祖母宅は通常開）
        self._motion_timestamps: deque[datetime] = deque(maxlen=100)
        self._last_candidate_at: datetime | None = None

    # ---- センサ入力 ----
    def feed_door(self, is_open: bool, ts: datetime) -> None:
        prev_open = self._is_door_open
        self._is_door_open = is_open
        if not is_open:
            self._last_door_closed_at = ts
        if prev_open and not is_open:
            log.info("[bath_detector] ドア閉検知 → 検知ウィンドウを開く")

    def feed_motion(self, ts: datetime) -> None:
        self._motion_timestamps.append(ts)

    async def feed_humidity(self, humidity: float, temperature: float, ts: datetime) -> None:
        self._humidity_history.append(_Sample(ts, humidity, temperature))
        await self._maybe_detect(ts)

    # ---- 検知ロジック ----
    async def _maybe_detect(self, now: datetime) -> None:
        if self._last_candidate_at and (now - self._last_candidate_at) < timedelta(minutes=COOLDOWN_MINUTES):
            return
        # ベースラインは「直近の湿度上昇開始前」の平均
        # シンプル版: 直近 HUMIDITY_HISTORY_MINUTES 分のうち、最古3点の平均をベースラインに
        cutoff = now - timedelta(minutes=HUMIDITY_HISTORY_MINUTES)
        recent = [s for s in self._humidity_history if s.timestamp >= cutoff]
        if len(recent) < 6:  # 最低60秒分のデータが必要
            return
        baseline_samples = recent[: max(3, len(recent) // 5)]  # 古い側 20% or 3点
        if not baseline_samples:
            return
        baseline_h = sum(s.humidity for s in baseline_samples) / len(baseline_samples)
        baseline_t = sum(s.temperature for s in baseline_samples) / len(baseline_samples)
        peak_h = max(s.humidity for s in recent)
        peak_t = max(s.temperature for s in recent)
        delta_h = peak_h - baseline_h
        delta_t = peak_t - baseline_t

        if delta_h < HUMIDITY_RISE_THRESHOLD:
            return

        # ピーク継続性チェック: ピーク値の80%以上を MIN_PEAK_DURATION_SECONDS 以上維持しているか
        threshold = baseline_h + delta_h * 0.6
        sustained = [s for s in recent if s.humidity >= threshold]
        if not sustained:
            return
        sustained_duration = (sustained[-1].timestamp - sustained[0].timestamp).total_seconds()
        if sustained_duration < MIN_PEAK_DURATION_SECONDS:
            return

        # モーション件数（直近 HUMIDITY_HISTORY_MINUTES 分）
        motion_count = sum(1 for t in self._motion_timestamps if t >= cutoff)

        # ドア閉判定: 直近 HUMIDITY_HISTORY_MINUTES 内にドアが閉まったか
        door_was_closed = (
            self._last_door_closed_at is not None
            and self._last_door_closed_at >= cutoff
        ) or (not self._is_door_open)

        active_person = self._get_active_person()

        candidate = BathCandidate(
            detected_at=now,
            door_was_closed=bool(door_was_closed),
            humidity_baseline=round(baseline_h, 1),
            humidity_peak=round(peak_h, 1),
            humidity_delta=round(delta_h, 1),
            temperature_delta=round(delta_t, 1),
            motion_count=motion_count,
            active_person_id=active_person,
        )
        log.info("[bath_detector] お風呂利用候補を検知: %s", candidate)

        # DB保存（confirmed_person_id=NULL）
        record_id = self._save_pending(candidate)
        self._last_candidate_at = now

        if self._on_candidate and record_id is not None:
            try:
                await self._on_candidate(candidate, record_id)
            except Exception as e:
                log.warning("on_candidate コールバックエラー: %s", e)

    def _save_pending(self, c: BathCandidate) -> int | None:
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """INSERT INTO bath_classifications
                       (detected_at, hour_of_day, door_was_closed,
                        humidity_baseline, humidity_peak, humidity_delta,
                        temperature_delta, motion_count, active_person_id)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    c.detected_at.strftime("%Y-%m-%d %H:%M:%S"),
                    c.detected_at.hour,
                    1 if c.door_was_closed else 0,
                    c.humidity_baseline,
                    c.humidity_peak,
                    c.humidity_delta,
                    c.temperature_delta,
                    c.motion_count,
                    c.active_person_id,
                ),
            )
            rid = cur.lastrowid
            conn.commit()
            conn.close()
            return rid
        except Exception as e:
            log.error("bath_classifications 保存失敗: %s", e)
            return None
