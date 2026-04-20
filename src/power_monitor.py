"""Tapo P115の電力監視ループ。

- 一定間隔で消費電力(W)を取得
- しきい値を超えたら「稼働開始」、下回ったら「稼働終了」と判定
- 炊飯器モードでは、1サイクル終了後にプラグを自動OFFロック (Phase 2)
- 2回目の稼働検知でLINE通知 (Phase 2)

まずはモニタリング本体のみ実装。ロック/通知は後続ステップ。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from kasa import SmartDevice, Discover

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "power_monitor.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("power_monitor")


class State(str, Enum):
    IDLE = "idle"
    RUNNING = "running"


@dataclass
class MonitorConfig:
    name: str
    ip: str
    threshold_w: float
    username: str
    password: str
    poll_interval: float = 5.0
    # しきい値を下回ってから「終了」と判定するまでの連続秒数
    idle_confirm_seconds: float = 30.0


class PowerMonitor:
    def __init__(self, cfg: MonitorConfig) -> None:
        self.cfg = cfg
        self.state: State = State.IDLE
        self._below_since: datetime | None = None
        self.cycle_count: int = 0

    async def _connect(self) -> SmartDevice:
        dev = await Discover.discover_single(
            self.cfg.ip,
            username=self.cfg.username,
            password=self.cfg.password,
        )
        await dev.update()
        return dev

    async def run(self) -> None:
        log.info("[%s] 監視開始 ip=%s threshold=%.0fW",
                 self.cfg.name, self.cfg.ip, self.cfg.threshold_w)
        dev = await self._connect()

        while True:
            try:
                await dev.update()
                emeter = dev.modules.get("Energy") or getattr(dev, "emeter_realtime", None)
                power_w = self._read_power(dev)
                await self._tick(power_w)
            except Exception as e:
                log.warning("[%s] 取得失敗: %s (再接続)", self.cfg.name, e)
                try:
                    dev = await self._connect()
                except Exception as e2:
                    log.error("[%s] 再接続失敗: %s", self.cfg.name, e2)

            await asyncio.sleep(self.cfg.poll_interval)

    @staticmethod
    def _read_power(dev: SmartDevice) -> float:
        # python-kasa 0.10系: Energyモジュール経由
        energy = dev.modules.get("Energy")
        if energy is not None:
            current = getattr(energy, "current_consumption", None)
            if current is not None:
                return float(current)
        # フォールバック
        status = getattr(dev, "emeter_realtime", None)
        if status:
            return float(status.get("power", 0.0))
        return 0.0

    async def _tick(self, power_w: float) -> None:
        now = datetime.now()
        log.debug("[%s] %.1fW state=%s", self.cfg.name, power_w, self.state)

        if self.state is State.IDLE:
            if power_w >= self.cfg.threshold_w:
                self.state = State.RUNNING
                self.cycle_count += 1
                self._below_since = None
                log.info("[%s] ▶ 稼働開始 (%.1fW) cycle=%d",
                         self.cfg.name, power_w, self.cycle_count)
                await self.on_start(power_w)
        else:  # RUNNING
            if power_w < self.cfg.threshold_w:
                if self._below_since is None:
                    self._below_since = now
                elif (now - self._below_since).total_seconds() >= self.cfg.idle_confirm_seconds:
                    self.state = State.IDLE
                    log.info("[%s] ■ 稼働終了 (%.1fW)", self.cfg.name, power_w)
                    self._below_since = None
                    await self.on_stop(power_w)
            else:
                self._below_since = None

    async def on_start(self, power_w: float) -> None:
        """サブクラスでオーバーライド。例: 2回目なら通知、ロック処理など。"""

    async def on_stop(self, power_w: float) -> None:
        """サブクラスでオーバーライド。例: 炊飯完了後の自動OFFロック。"""
