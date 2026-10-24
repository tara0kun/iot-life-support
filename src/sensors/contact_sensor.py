"""H100ハブ経由でTapo T110開閉センサー / T100モーションセンサーを監視するモジュール。

python-kasaでH100のchild devicesから ContactSensor / MotionSensor を読み取る。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Awaitable

from .hub_discovery import resolve_hub

log = logging.getLogger("sensors.contact")


@dataclass
class ContactEvent:
    device_id: str
    alias: str
    is_open: bool
    timestamp: datetime


@dataclass
class ContactSensorConfig:
    hub_ip: str
    username: str
    password: str
    poll_interval: float = 5.0


@dataclass
class BatteryStatus:
    alias: str
    device_id: str
    is_low: bool
    rssi: int | None


# 電池を見にいく間隔。**切れる前に気づくための仕組みなので、頻度は要らない。**
# ハブが出す battery_low は boolean で、残量%は公開されていない（実測で
# battery_level feature は存在しなかった）。1時間に1回あれば十分。
BATTERY_CHECK_INTERVAL_SECONDS = 3600


class ContactSensorMonitor:
    def __init__(
        self,
        cfg: ContactSensorConfig,
        on_change: Callable[[ContactEvent], Awaitable[None]] | None = None,
        on_motion: Callable[[ContactEvent], Awaitable[None]] | None = None,
        on_battery_low: Callable[[BatteryStatus], Awaitable[None]] | None = None,
    ):
        self.cfg = cfg
        self._on_change = on_change
        self._on_motion = on_motion
        self._on_battery_low = on_battery_low
        self._running = False
        self._last_state: dict[str, bool] = {}
        self._last_battery_check = 0.0

    def _read_battery(self, child) -> BatteryStatus | None:
        """子機の電池状態を読む。読めなければ None。

        python-kasa の版によってモジュール属性名が変わるので feature 経由で引く。
        実測（2026-10-23, kasa 0.10.2）では T110/T100 に battery_level は無く、
        battery_low（boolean）と rssi だけが取れた。
        """
        feats = getattr(child, "features", {}) or {}
        f = feats.get("battery_low")
        if f is None:
            return None
        try:
            is_low = bool(f.value)
        except Exception:
            return None
        rssi = None
        rf = feats.get("rssi")
        if rf is not None:
            try:
                rssi = int(rf.value)
            except Exception:
                rssi = None
        return BatteryStatus(
            alias=child.alias, device_id=child.device_id, is_low=is_low, rssi=rssi
        )

    async def _check_batteries(self, hub) -> None:
        """**電池切れを、センサーが黙る前に知らせる。**

        これが無いと、電池が切れてもそのセンサーが30時間以上無反応になるまで
        誰も気づけない（health_check のしきい値）。脱衣所のモーションセンサーは
        入浴の確定と「浴室30分動きなし」の緊急通知の両方を担っているので、
        黙ってから気づくのでは遅い。2026-10-23 の点検で実際に低電池だった。
        """
        for child in hub.children:
            st = self._read_battery(child)
            if st is None:
                continue
            if st.is_low:
                log.warning("[%s] 電池残量が少ない (rssi=%s)", st.alias, st.rssi)
                if self._on_battery_low:
                    try:
                        await self._on_battery_low(st)
                    except Exception as e:
                        log.error("低電池の通知に失敗: %s: %s", st.alias, e)

    async def _dispatch(self, event: ContactEvent, handler) -> None:
        """イベントを1件渡す。失敗しても呼び出し側のループを壊さない。

        **1台の失敗で他の子機を巻き込まない。** 以前はコールバックの例外が
        run() の try まで飛んでいたため、(1) そのポーリング周回で残りの子機が
        丸ごと読み飛ばされ、(2) ハブの再接続まで走っていた。

        **そして失敗は黙らせない。** 書き込めなかったイベントは復元できないので、
        ERROR で「何を失ったか」を残す。ロック競合そのものの根治は
        journal_mode=WAL 側の話（Issue #16）。
        """
        if handler is None:
            return
        try:
            await handler(event)
        except Exception as e:
            log.error(
                "イベントの記録に失敗（この1件は失われる）: [%s] %s: %s",
                event.alias, "OPEN" if event.is_open else "CLOSED", e,
            )

    async def _connect_hub(self):
        # キャッシュ/設定IP/ブロードキャスト探索の順でH100を解決
        dev = await resolve_hub(self.cfg.hub_ip, self.cfg.username, self.cfg.password)
        if dev is None:
            raise RuntimeError("H100が見つかりません")
        return dev

    async def run(self) -> None:
        log.info("T110監視開始 (configured hub=%s)", self.cfg.hub_ip)
        self._running = True
        hub = None
        for attempt in range(5):
            try:
                hub = await self._connect_hub()
                break
            except Exception as e:
                log.warning("H100接続試行 %d/5 失敗: %s", attempt + 1, e)
                await asyncio.sleep(5)
        if hub is None:
            log.error("H100に接続できません。T110監視をスキップします。")
            return

        while self._running:
            try:
                await hub.update()
                # 既存の接続を使い回す。別に張ると5秒ポーリングと競合する。
                now_mono = time.monotonic()
                if now_mono - self._last_battery_check >= BATTERY_CHECK_INTERVAL_SECONDS:
                    self._last_battery_check = now_mono
                    await self._check_batteries(hub)
                for child in hub.children:
                    dev_id = child.device_id
                    contact = child.modules.get("ContactSensor")
                    motion = child.modules.get("MotionSensor")

                    if contact is not None:
                        is_open = getattr(contact, "is_open", None)
                        if is_open is None:
                            continue
                        prev = self._last_state.get(dev_id)
                        # **状態の更新をコールバックより先に行う。**
                        # 逆順だと、コールバック（最終的に DB 書き込み）が
                        # `database is locked` で落ちたときに _last_state が
                        # 取り残され、続く逆方向の遷移まで「変化なし」と判定されて
                        # 無言で消える。1回の書き込み失敗で2イベント失っていた
                        # （2026-09-26 の実例: ハブ側に open/close があるのに
                        #  DB にはどちらも無い）。Issue #22
                        self._last_state[dev_id] = is_open
                        if prev is not None and prev != is_open:
                            event = ContactEvent(
                                device_id=dev_id,
                                alias=child.alias,
                                is_open=is_open,
                                timestamp=datetime.now(),
                            )
                            log.info("[%s] %s", child.alias, "OPEN" if is_open else "CLOSED")
                            await self._dispatch(event, self._on_change)

                    elif motion is not None:
                        # T100モーション: motion_detected が False→True に立ち上がった瞬間に発火
                        detected = getattr(motion, "motion_detected", None)
                        if detected is None:
                            continue
                        prev = self._last_state.get(dev_id, False)
                        self._last_state[dev_id] = detected  # 接触側と同じ理由で先に更新
                        if detected and not prev:
                            event = ContactEvent(
                                device_id=dev_id,
                                alias=child.alias,
                                is_open=True,  # motion=Trueのみ意味あり
                                timestamp=datetime.now(),
                            )
                            log.info("[%s] MOTION", child.alias)
                            await self._dispatch(event, self._on_motion or self._on_change)
            except Exception as e:
                log.warning("T110/T100取得失敗: %s (再接続)", e)
                try:
                    hub = await self._connect_hub()
                except Exception as e2:
                    log.error("再接続失敗: %s", e2)
            await asyncio.sleep(self.cfg.poll_interval)

    def stop(self) -> None:
        self._running = False
