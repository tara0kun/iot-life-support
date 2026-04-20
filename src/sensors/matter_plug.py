"""Matter経由でTapo P110Mの電力を監視するモジュール。

python-matter-serverのWebSocket APIを使い、定期的に電力データを取得する。
しきい値を超えたら/下回ったらコールバックを呼ぶ。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable

import aiohttp

log = logging.getLogger("sensors.matter_plug")

MATTER_WS = "ws://localhost:5580/ws"


@dataclass
class PlugReading:
    power_w: float
    on_off: bool
    timestamp: datetime = field(default_factory=datetime.now)
    voltage_v: float | None = None
    current_a: float | None = None


@dataclass
class MatterPlugConfig:
    name: str
    node_id: int = 1
    endpoint: int = 1
    poll_interval: float = 5.0
    threshold_w: float = 10.0
    idle_confirm_seconds: float = 30.0


class MatterPlugMonitor:
    def __init__(
        self,
        cfg: MatterPlugConfig,
        on_start: Callable[[str, PlugReading], Awaitable[None]] | None = None,
        on_stop: Callable[[str, PlugReading], Awaitable[None]] | None = None,
        on_reading: Callable[[str, PlugReading], Awaitable[None]] | None = None,
    ):
        self.cfg = cfg
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_reading = on_reading
        self._running = False
        self._active = False
        self._below_since: datetime | None = None
        self.cycle_count: int = 0

    async def _get_node_attrs(self, session: aiohttp.ClientSession) -> dict:
        ws = await session.ws_connect(MATTER_WS)
        try:
            await asyncio.wait_for(ws.receive_json(), timeout=5)
            await ws.send_json({"message_id": "r", "command": "get_nodes"})
            while True:
                resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
                if resp.get("message_id") == "r":
                    break
            nodes = resp.get("result", [])
            for n in nodes:
                if n["node_id"] == self.cfg.node_id:
                    return n.get("attributes", {})
            return {}
        finally:
            await ws.close()

    def _parse_reading(self, attrs: dict) -> PlugReading:
        ep = str(self.cfg.endpoint)
        power_mw = attrs.get(f"{ep}/144/8", 0) or 0
        on_off = attrs.get(f"{ep}/6/0", False)
        return PlugReading(
            power_w=power_mw / 1000.0,
            on_off=bool(on_off),
        )

    async def run(self) -> None:
        log.info("[%s] 電力監視開始 (Matter node=%d, threshold=%.0fW)",
                 self.cfg.name, self.cfg.node_id, self.cfg.threshold_w)
        self._running = True

        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    attrs = await self._get_node_attrs(session)
                    reading = self._parse_reading(attrs)
                    if self._on_reading:
                        await self._on_reading(self.cfg.name, reading)
                    await self._tick(reading)
                except Exception as e:
                    log.warning("[%s] 取得失敗: %s", self.cfg.name, e)
                await asyncio.sleep(self.cfg.poll_interval)

    async def _tick(self, r: PlugReading) -> None:
        now = datetime.now()
        if not self._active:
            if r.power_w >= self.cfg.threshold_w:
                self._active = True
                self.cycle_count += 1
                self._below_since = None
                log.info("[%s] ▶ 稼働開始 %.1fW cycle=%d",
                         self.cfg.name, r.power_w, self.cycle_count)
                if self._on_start:
                    await self._on_start(self.cfg.name, r)
        else:
            if r.power_w < self.cfg.threshold_w:
                if self._below_since is None:
                    self._below_since = now
                elif (now - self._below_since).total_seconds() >= self.cfg.idle_confirm_seconds:
                    self._active = False
                    self._below_since = None
                    log.info("[%s] ■ 稼働終了 %.1fW", self.cfg.name, r.power_w)
                    if self._on_stop:
                        await self._on_stop(self.cfg.name, r)
            else:
                self._below_since = None

    def stop(self) -> None:
        self._running = False

