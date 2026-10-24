"""入浴見守りの状態が再起動をまたいで引き継がれるかのテスト。

**入浴中にプロセスが落ちると見守りが消える**のが元の問題（Issue #27）。
ドア閉のイベントは過ぎているので、復帰後に自力で監視を再開できないと
その回は最後まで誰も見ていない状態になる。

pytest-asyncio は入れていない（依存を増やさない方針）。
標準の asyncio.run() で1テスト1ループを回す。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from src.bath_monitor import (
    CONFIRM_MOTION_WINDOW_SECONDS,
    MAX_RESUME_HOURS,
    BathMonitor,
    _load_state,
    _save_state,
)


def _run(coro_factory):
    """コルーチンを1つ走らせる。残ったタスクは閉じる前に片付ける。"""

    async def wrapper():
        try:
            return await coro_factory()
        finally:
            for t in asyncio.all_tasks() - {asyncio.current_task()}:
                t.cancel()

    return asyncio.run(wrapper())


def test_door_closed_persists_state(temp_db):
    """ドアが閉まった時点で状態が DB に残る。"""

    async def body():
        m = BathMonitor()
        await m.door_closed()

    _run(body)

    state = _load_state()
    assert state is not None
    assert state["in_bath"] == 1
    assert state["confirmed"] == 0
    assert state["bath_start"] is not None


def test_motion_persists_confirmation(temp_db):
    """モーションで確定したことが DB に残る。"""

    async def body():
        m = BathMonitor()
        await m.door_closed()
        await m.motion_detected()

    _run(body)

    state = _load_state()
    assert state["confirmed"] == 1
    assert state["last_motion"] is not None


def test_door_opened_clears_state(temp_db):
    """入浴が終われば状態は残らない（次の起動で復元しない）。"""

    async def body():
        m = BathMonitor()
        await m.door_closed()
        await m.motion_detected()
        await m.door_opened()

    _run(body)

    assert _load_state()["in_bath"] == 0


def test_restore_resumes_confirmed_bath(temp_db):
    """確定済みの入浴は復元され、監視タスクが再開する。"""
    started = datetime.now() - timedelta(minutes=10)
    _save_state(True, started, started, False, True)

    async def body():
        m = BathMonitor()
        assert await m.restore() is True
        assert m.is_in_bath is True
        assert m._confirmed is True
        assert m._monitor_task is not None

    _run(body)


def test_restore_keeps_alert_pending_when_motion_is_stale(temp_db):
    """落ちている間に動きが止まっていたら、復帰後もアラート前の状態を保つ。

    ここが本体。再起動で見守りが消えると、この通知が永遠に出ない。
    復元後は「しきい値超過・未送信」の状態になっている必要がある。
    """
    started = datetime.now() - timedelta(minutes=50)
    last_motion = datetime.now() - timedelta(minutes=45)
    _save_state(True, started, last_motion, False, True)

    async def body():
        m = BathMonitor(alert_minutes=30)
        assert await m.restore() is True
        assert m._alert_sent is False
        assert m._last_motion == last_motion
        elapsed = (datetime.now() - m._last_motion).total_seconds() / 60
        assert elapsed >= m.alert_minutes

    _run(body)


def test_restore_skips_stale_bath(temp_db):
    """古すぎる入浴は引き継がない（誰もいない浴室を見張り続けない）。"""
    started = datetime.now() - timedelta(hours=MAX_RESUME_HOURS + 1)
    _save_state(True, started, started, False, True)

    async def body():
        m = BathMonitor()
        assert await m.restore() is False
        assert m.is_in_bath is False

    _run(body)
    assert _load_state()["in_bath"] == 0


def test_restore_cancels_unconfirmed_expired_phase1(temp_db):
    """未確定のまま Phase1 の猶予が尽きていたら、入浴として扱わない。

    ドアが閉まっただけでモーションが無かった回（家族が後で閉めた等）を
    復帰時に入浴として復活させない。
    """
    started = datetime.now() - timedelta(seconds=CONFIRM_MOTION_WINDOW_SECONDS + 60)
    _save_state(True, started, None, False, False)

    async def body():
        m = BathMonitor()
        assert await m.restore() is True  # いったん復元はする
        # Phase1 の残りが尽きているので、タイマーはキャンセル側へ落ちる
        await asyncio.wait_for(m._monitor_task, timeout=5)
        assert m.is_in_bath is False

    _run(body)
    assert _load_state()["in_bath"] == 0


def test_restore_without_saved_state(temp_db):
    """保存が無ければ何もしない。"""

    async def body():
        m = BathMonitor()
        assert await m.restore() is False
        assert m.is_in_bath is False

    _run(body)


def test_save_failure_does_not_stop_monitoring(temp_db, monkeypatch):
    """DB が書けなくても監視は止めない（永続化は保険であって本体ではない）。"""

    def boom(*a, **kw):
        raise RuntimeError("DBロック競合を模擬")

    monkeypatch.setattr("src.db.transaction", boom)

    async def body():
        m = BathMonitor()
        await m.door_closed()  # 例外が外に出ないこと
        assert m.is_in_bath is True

    _run(body)
