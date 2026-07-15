"""お風呂監視モジュール。

T110（浴室ドア開閉）+ T100（脱衣所モーション）を組み合わせて:
  - 入浴開始/終了をイベントとして記録
  - 長時間動きがない場合に家族へ緊急LINE通知

検知ロジック (2026-07-15 更新):
  1. T110 CLOSED → 入浴「暫定」開始（Phase1: 確定待ち、on_bath_start はまだ呼ばない）
  2. Phase1 内で T100 モーション検知 → 入浴確定、on_bath_start 発火、Phase2 (通常監視) へ
  3. Phase1 内でモーション無し (CONFIRM_MOTION_WINDOW_SECONDS = 5分) → 判定キャンセル
     ← 「入浴後にドアを開けっ放し → 家族が後で閉めた」時の誤検知を防ぐ
  4. Phase2: ALERT_MINUTES 経過で動きなし → 緊急通知
  5. T110 OPEN → 入浴終了 (確定時のみ on_bath_end 発火)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Awaitable

log = logging.getLogger("bath_monitor")

# デフォルト: 30分動きがなければアラート
ALERT_MINUTES = 30

# 入浴確定に必要な「ドア閉後の初動モーション」猶予秒数。
# この時間内にモーションが来なければ「入浴じゃなかった」としてキャンセル。
CONFIRM_MOTION_WINDOW_SECONDS = 5 * 60


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
        self._confirmed = False   # ★ Phase1 (ドア閉直後) を通過し、モーションで入浴確定したか
        self._monitor_task: asyncio.Task | None = None

    async def door_closed(self) -> None:
        """浴室ドアが閉まった → 入浴「暫定」開始 (Phase1)。
        CONFIRM_MOTION_WINDOW_SECONDS 以内にモーションが来なければキャンセルする。
        """
        if self._in_bath:
            return
        self._in_bath = True
        self._bath_start = datetime.now()
        self._last_motion = None   # ★ 変更: 開始時点はモーション未取得 (Phase1 で待つ)
        self._alert_sent = False
        self._confirmed = False
        log.info("入浴 (暫定) 開始: %d分以内モーション待機", CONFIRM_MOTION_WINDOW_SECONDS // 60)
        # on_bath_start は Phase1 通過 (モーション初検知) まで呼ばない
        self._monitor_task = asyncio.create_task(self._watch_timer())

    async def door_opened(self) -> None:
        """浴室ドアが開いた → 入浴終了 (確定していた場合のみイベント発火)。"""
        if not self._in_bath:
            return
        duration_min = 0.0
        if self._bath_start:
            duration_min = (datetime.now() - self._bath_start).total_seconds() / 60
        was_confirmed = self._confirmed
        self._in_bath = False
        self._bath_start = None
        self._last_motion = None
        self._confirmed = False
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
        if was_confirmed:
            log.info("入浴終了 (%.1f分)", duration_min)
            if self._on_bath_end:
                await self._on_bath_end(duration_min)
        else:
            # 未確定のままドアが開いた = Phase1 内で誰かがドア開けた or 一度も入浴確定しなかった
            log.info("暫定入浴のままドア開 → 入浴イベント記録スキップ (%.1f分)", duration_min)

    async def motion_detected(self) -> None:
        """脱衣所/浴室でモーション検知 → 生存確認。
        Phase1 (未確定) 中の初モーションで入浴確定 → on_bath_start 発火。
        """
        if not self._in_bath:
            return
        self._last_motion = datetime.now()
        if not self._confirmed:
            self._confirmed = True
            log.info("入浴確定: Phase1 内でモーション検知")
            if self._on_bath_start:
                await self._on_bath_start()
        else:
            log.debug("浴室モーション検知 (生存確認)")

    async def _watch_timer(self) -> None:
        """Phase1: モーション猶予時間内に確定しなければキャンセル。
        Phase2: 通常の ALERT_MINUTES 経過モーションなしアラート監視。
        """
        try:
            # Phase1: 確定待ち (5分)
            phase1_end = False
            for _ in range(CONFIRM_MOTION_WINDOW_SECONDS // 10):
                await asyncio.sleep(10)
                if not self._in_bath:
                    return  # ドア開でキャンセル済
                if self._confirmed:
                    phase1_end = True
                    break
            if not phase1_end:
                # Phase1 タイムアウト → 判定キャンセル
                # (祖父がドア開けっ放し → 家族が後で閉めた パターンの誤検知を防ぐ)
                log.info("入浴誤検知: ドア閉後 %d 分モーション無しでキャンセル",
                         CONFIRM_MOTION_WINDOW_SECONDS // 60)
                self._in_bath = False
                self._bath_start = None
                self._confirmed = False
                return

            # Phase2: 通常の 30分アラート監視
            while self._in_bath:
                await asyncio.sleep(60)
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
