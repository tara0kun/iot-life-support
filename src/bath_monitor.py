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

# 再起動から復帰したとき、これより古い入浴は引き継がない。
# ドア閉から長時間経っていれば、本人はとっくに出ていてドア開を取りこぼしたか、
# センサー側が固着している。古い状態を復元すると「誰もいない浴室」を
# 見張り続け、いずれ誤アラートを出す。
MAX_RESUME_HOURS = 3


def _save_state(in_bath: bool, bath_start: datetime | None,
                last_motion: datetime | None, alert_sent: bool,
                confirmed: bool) -> None:
    """見守りの進行状態を DB に書く。**失敗しても監視は続ける。**

    ここで例外を上げるとセンサーの取り込みループごと巻き込むので、
    握りつぶしてログだけ残す。永続化は保険であって本体ではない。
    """
    try:
        from .db import transaction
        with transaction() as conn:
            conn.execute(
                """INSERT INTO bath_monitor_state
                       (id, in_bath, bath_start, last_motion, alert_sent, confirmed, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(id) DO UPDATE SET
                       in_bath=excluded.in_bath,
                       bath_start=excluded.bath_start,
                       last_motion=excluded.last_motion,
                       alert_sent=excluded.alert_sent,
                       confirmed=excluded.confirmed,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    int(in_bath),
                    bath_start.isoformat() if bath_start else None,
                    last_motion.isoformat() if last_motion else None,
                    int(alert_sent),
                    int(confirmed),
                ),
            )
    except Exception as e:
        log.warning("入浴見守り状態の保存に失敗: %s", e)


def _load_state() -> dict | None:
    """保存された進行状態を読む。無ければ None。"""
    try:
        from .db import get_conn
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT in_bath, bath_start, last_motion, alert_sent, confirmed "
                "FROM bath_monitor_state WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.warning("入浴見守り状態の読み出しに失敗: %s", e)
        return None


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class BathMonitor:
    def __init__(
        self,
        alert_minutes: int = ALERT_MINUTES,
        on_bath_start: Callable[[], Awaitable[None]] | None = None,
        on_bath_end: Callable[[float], Awaitable[None]] | None = None,
        on_alert_resolved: Callable[[], Awaitable[None]] | None = None,
        on_alert: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.alert_minutes = alert_minutes
        self._on_bath_start = on_bath_start
        self._on_bath_end = on_bath_end
        self._on_alert_resolved = on_alert_resolved
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
        self._persist()
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
        self._persist()
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

        # **動きの再開はアラートの解決である。**
        # このアラートの根拠は「N分間動きがない」なので、動きが戻れば
        # 前提が消える。以前はここで何もしておらず、ドアが開いて bath_end が
        # 出るまで pending が未応答のまま残り、その間 recheck_pending が
        # 再通知を続けていた。実際に、モーション再開後も通知が続き、
        # 誰も応答しないまま打ち切りに至った事例がある。
        # _alert_sent は戻す。再び静かになれば改めて鳴るべきなので。
        if self._alert_sent:
            self._alert_sent = False
            log.info("浴室アラート後にモーション再開 → 解決とみなす")
            if self._on_alert_resolved:
                await self._on_alert_resolved()

        if not self._confirmed:
            self._confirmed = True
            log.info("入浴確定: Phase1 内でモーション検知")
            self._persist()
            if self._on_bath_start:
                await self._on_bath_start()
        else:
            log.debug("浴室モーション検知 (生存確認)")
            self._persist()

    async def _watch_timer(self, phase1_remaining: float = CONFIRM_MOTION_WINDOW_SECONDS) -> None:
        """Phase1: モーション猶予時間内に確定しなければキャンセル。
        Phase2: 通常の ALERT_MINUTES 経過モーションなしアラート監視。

        phase1_remaining: Phase1 の残り秒数。再起動から復帰したときは
        既に経過した分を差し引いた値が渡る（0以下なら Phase1 は終了扱い）。
        """
        try:
            # Phase1: 確定待ち (既定5分、復帰時は残り時間)
            # 未確定のまま残り時間が尽きていれば、ループを回さず下のキャンセルへ落ちる
            phase1_end = self._confirmed
            for _ in range(max(0, int(phase1_remaining // 10))):
                if phase1_end:
                    break
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
                self._persist()
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
                    self._persist()
                    if self._on_alert:
                        await self._on_alert(elapsed)
        except asyncio.CancelledError:
            pass

    def _persist(self) -> None:
        _save_state(self._in_bath, self._bath_start, self._last_motion,
                    self._alert_sent, self._confirmed)

    async def restore(self) -> bool:
        """保存された進行状態から見守りを再開する。再開したら True。

        **これが無いと、入浴中にプロセスが落ちた回は見守りが消える。**
        ドア閉のイベントは過ぎているので、復帰後に自力で監視を始める
        きっかけが他に無い。
        """
        state = _load_state()
        if not state or not state["in_bath"]:
            return False

        bath_start = _parse(state["bath_start"])
        if bath_start is None:
            _save_state(False, None, None, False, False)
            return False

        elapsed = (datetime.now() - bath_start).total_seconds()
        if elapsed > MAX_RESUME_HOURS * 3600:
            log.info("保存された入浴が古すぎるため引き継がない (%.1f時間前)", elapsed / 3600)
            _save_state(False, None, None, False, False)
            return False

        self._in_bath = True
        self._bath_start = bath_start
        self._last_motion = _parse(state["last_motion"])
        self._alert_sent = bool(state["alert_sent"])
        self._confirmed = bool(state["confirmed"])

        phase1_remaining = CONFIRM_MOTION_WINDOW_SECONDS - elapsed
        log.warning(
            "入浴見守りを復元: 経過%.0f分 確定=%s アラート済=%s",
            elapsed / 60, self._confirmed, self._alert_sent,
        )
        self._monitor_task = asyncio.create_task(self._watch_timer(phase1_remaining))
        return True

    @property
    def is_in_bath(self) -> bool:
        return self._in_bath

    @property
    def bath_duration_minutes(self) -> float | None:
        if self._in_bath and self._bath_start:
            return (datetime.now() - self._bath_start).total_seconds() / 60
        return None
