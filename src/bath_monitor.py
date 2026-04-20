"""お風呂監視モジュール。

T110（浴室ドア開閉）+ T100（脱衣所モーション）を組み合わせて:
  - 入浴開始/終了をイベントとして記録
  - 長時間動きがない場合に家族へ緊急LINE通知

検知ロジック:
  1. T110 CLOSED → 入浴開始（タイマー開始）
  2. T100 モーション検知 → 生存確認（タイマーリセット）
  3. ドア閉 + ALERT_MINUTES 経過で動きなし → 緊急通知
  4. T110 OPEN → 入浴終了、記録帳に「おふろ」イベント記録
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Awaitable

log = logging.getLogger("bath_monitor")

# デフォルト: 30分動きがなければアラート
ALERT_MINUTES = 30


class BathMonitor:
    def __init__(
        self,
        alert_minutes: int = ALERT_MINUTES,
        on_bath_start: Callable[[], Awaitable[None]] | None = None,
        on_bath_end: Callable[[float], Awaitable[None]] | None = None,
        on_alert: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.alert_minutes = alert_minutes
        self._on_bath_start = on_bath_start
        self._on_bath_end = on_bath_end
        self._on_alert = on_alert

        self._in_bath = False
        self._bath_start: datetime | None = None
        self._last_motion: datetime | None = None
        self._alert_sent = False
        self._monitor_task: asyncio.Task | None = None

    async def door_closed(self) -> None:
        """浴室ドアが閉まった → 入浴開始。"""
        if self._in_bath:
            return
        self._in_bath = True
        self._bath_start = datetime.now()
        self._last_motion = datetime.now()
        self._alert_sent = False
        log.info("入浴開始")
        if self._on_bath_start:
            await self._on_bath_start()
        # タイマー監視開始
        self._monitor_task = asyncio.create_task(self._watch_timer())

    async def door_opened(self) -> None:
        """浴室ドアが開いた → 入浴終了。"""
        if not self._in_bath:
            return
        duration_min = 0.0
        if self._bath_start:
            duration_min = (datetime.now() - self._bath_start).total_seconds() / 60
        self._in_bath = False
        self._bath_start = None
        self._last_motion = None
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
        log.info("入浴終了 (%.1f分)", duration_min)
        if self._on_bath_end:
            await self._on_bath_end(duration_min)

    async def motion_detected(self) -> None:
        """脱衣所/浴室でモーション検知 → 生存確認。"""
        if not self._in_bath:
            return
        self._last_motion = datetime.now()
        log.debug("浴室モーション検知 (生存確認)")

    async def _watch_timer(self) -> None:
        """入浴中にモーションがなければアラートを送る。"""
        try:
            while self._in_bath:
                await asyncio.sleep(60)  # 1分ごとにチェック
                if not self._in_bath:
                    break
                if self._last_motion is None:
                    continue
                elapsed = (datetime.now() - self._last_motion).total_seconds() / 60
                if elapsed >= self.alert_minutes and not self._alert_sent:
                    self._alert_sent = True
                    log.warning("浴室アラート: %.0f分間動きなし!", elapsed)
                    if self._on_alert:
                        await self._on_alert(elapsed)
        except asyncio.CancelledError:
            pass

    @property
    def is_in_bath(self) -> bool:
        return self._in_bath

    @property
    def bath_duration_minutes(self) -> float | None:
        if self._in_bath and self._bath_start:
            return (datetime.now() - self._bath_start).total_seconds() / 60
        return None
