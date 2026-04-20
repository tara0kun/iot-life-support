"""H100ハブ経由でTapo T110開閉センサーを監視するモジュール。

python-kasaでH100のchild devicesからContactSensor/TriggerLogsを読み取る。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Awaitable

from kasa import Discover

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
    ):
        self.cfg = cfg
        self._on_change = on_change
        self._running = False
        self._last_state: dict[str, bool] = {}

    async def _connect_hub(self):
        dev = await Discover.discover_single(
            self.cfg.hub_ip,
            username=self.cfg.username,
            password=self.cfg.password,
        )
        await dev.update()
        return dev

    async def run(self) -> None:
        log.info("T110監視開始 (hub=%s)", self.cfg.hub_ip)
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
                    contact = child.modules.get("ContactSensor")
                    if contact is None:
                        continue
                    is_open = getattr(contact, "is_open", None)
                    if is_open is None:
                        continue
                    dev_id = child.device_id
                    prev = self._last_state.get(dev_id)
                    if prev is not None and prev != is_open:
                        event = ContactEvent(
                            device_id=dev_id,
                            alias=child.alias,
                            is_open=is_open,
                            timestamp=datetime.now(),
                        )
                        log.info("[%s] %s", child.alias, "OPEN" if is_open else "CLOSED")
                        if self._on_change:
                            await self._on_change(event)
                    self._last_state[dev_id] = is_open
            except Exception as e:
                log.warning("T110取得失敗: %s (再接続)", e)
                try:
                    hub = await self._connect_hub()
                except Exception as e2:
                    log.error("再接続失敗: %s", e2)
            await asyncio.sleep(self.cfg.poll_interval)

    def stop(self) -> None:
        self._running = False
