"""H100ハブ経由でTapo T110開閉センサー / T100モーションセンサーを監視するモジュール。

python-kasaでH100のchild devicesから ContactSensor / MotionSensor を読み取る。
"""
from __future__ import annotations

import asyncio
import logging
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


class ContactSensorMonitor:
    def __init__(
        self,
        cfg: ContactSensorConfig,
        on_change: Callable[[ContactEvent], Awaitable[None]] | None = None,
        on_motion: Callable[[ContactEvent], Awaitable[None]] | None = None,
    ):
        self.cfg = cfg
        self._on_change = on_change
        self._on_motion = on_motion
        self._running = False
        self._last_state: dict[str, bool] = {}

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
