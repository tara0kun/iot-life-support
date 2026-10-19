"""炊飯器の蓋が open で固着したときに検知できるかのテスト。

固着は無言の障害で、2026-10-19 の実測では最長9日間気づけなかった。
`open` 遷移が来ないこと自体が異常なので、「イベントが無い」ことを
根拠に発火させる必要がある。その境界をここで固定する。
"""
from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta

import pytest

FROZEN = datetime(2026, 6, 15, 14, 0, 0)


@pytest.fixture
def anomaly(temp_db, monkeypatch):
    """anomaly_check を一時DB・通知スタブ・フラグ隔離の状態で読み込む。"""
    import scripts.anomaly_check as ac

    importlib.reload(ac)

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(ac, "send_actionable_notification",
                        lambda cat, ctx, msg: sent.append((cat, ctx, msg)))
    monkeypatch.setattr(ac, "get_bool", lambda key, default=True: True)
    # 「今日は通知済み」フラグは本番の data/anomaly_flags を汚さないよう隔離する
    flags: set[str] = set()
    monkeypatch.setattr(ac, "_already_notified_today", lambda key: key in flags)
    monkeypatch.setattr(ac, "_mark_notified", lambda key: flags.add(key))
    ac._test_sent = sent
    ac._test_flags = flags
    return ac


def _put_lid_event(db_path, when: datetime, event_type: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events(source, event_type, started_at) VALUES('rice_cooker_lid', ?, ?)",
        (event_type, when.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def test_open_for_long_time_alerts(anomaly, temp_db, frozen_now):
    """しきい値(3時間)を超えて open のままなら通知する。"""
    _put_lid_event(temp_db, FROZEN - timedelta(hours=9), "open")

    anomaly.check_rice_lid_stuck()

    assert len(anomaly._test_sent) == 1
    category, _ctx, msg = anomaly._test_sent[0]
    assert category == "anomaly_rice_lid_stuck"
    assert "蓋が開いたまま" in msg
    assert "9時間経過" in msg


def test_open_within_threshold_is_silent(anomaly, temp_db, frozen_now):
    """短時間開いているだけなら通知しない（普通に使っている状態）。"""
    _put_lid_event(temp_db, FROZEN - timedelta(minutes=30), "open")

    anomaly.check_rice_lid_stuck()

    assert anomaly._test_sent == []


def test_closed_state_is_silent(anomaly, temp_db, frozen_now):
    """直近が close なら固着していないので通知しない。"""
    _put_lid_event(temp_db, FROZEN - timedelta(hours=20), "open")
    _put_lid_event(temp_db, FROZEN - timedelta(hours=19), "close")

    anomaly.check_rice_lid_stuck()

    assert anomaly._test_sent == []


def test_no_events_at_all_is_silent(anomaly, temp_db, frozen_now):
    """蓋イベントが1件も無い場合は、この検知の対象外（別問題として扱う）。"""
    anomaly.check_rice_lid_stuck()

    assert anomaly._test_sent == []


def test_only_notified_once_per_day(anomaly, temp_db, frozen_now):
    """同じ固着で鳴り続けない。家族が通知を無視する習慣をつけさせない。"""
    _put_lid_event(temp_db, FROZEN - timedelta(hours=9), "open")

    anomaly.check_rice_lid_stuck()
    anomaly.check_rice_lid_stuck()
    anomaly.check_rice_lid_stuck()

    assert len(anomaly._test_sent) == 1


def test_disabled_by_master_switch(anomaly, temp_db, frozen_now, monkeypatch):
    """異常検知OFFのときは発火しない。"""
    monkeypatch.setattr(anomaly, "get_bool", lambda key, default=True: False)
    _put_lid_event(temp_db, FROZEN - timedelta(hours=9), "open")

    anomaly.check_rice_lid_stuck()

    assert anomaly._test_sent == []


def test_threshold_boundary(anomaly, temp_db, frozen_now):
    """しきい値ちょうど(3時間)では発火する。1秒手前では発火しない。"""
    _put_lid_event(temp_db, FROZEN - timedelta(hours=3), "open")
    anomaly.check_rice_lid_stuck()
    assert len(anomaly._test_sent) == 1

    anomaly._test_sent.clear()
    anomaly._test_flags.clear()
    conn = sqlite3.connect(temp_db)
    conn.execute("DELETE FROM events")
    conn.commit()
    conn.close()

    _put_lid_event(temp_db, FROZEN - timedelta(hours=3) + timedelta(seconds=1), "open")
    anomaly.check_rice_lid_stuck()
    assert anomaly._test_sent == []


def test_premise_gone_closes_pending(temp_db, monkeypatch):
    """蓋が閉まったら、未応答の固着アラートを自動で閉じる。

    閉じないと CRITICAL でないとはいえ再通知が残り、解決済みの事象について
    鳴り続ける。
    """
    import scripts.recheck_pending as rp

    importlib.reload(rp)

    created = datetime(2026, 6, 15, 10, 0, 0)
    _put_lid_event(temp_db, created - timedelta(hours=5), "open")

    # 未応答の固着アラートが1件ある状態を作る（context_key は発生日）
    conn = sqlite3.connect(temp_db)
    conn.execute(
        """INSERT INTO pending_notifications(notification_type, context_key, message)
           VALUES('anomaly_rice_lid_stuck', ?, 'テスト用')""",
        (created.strftime("%Y-%m-%d"),),
    )
    conn.commit()
    conn.close()

    # まだ閉まっていない → 前提は残っている
    assert rp._close_if_premise_gone("anomaly_rice_lid_stuck", created) is False

    # 閉まった → 前提が消えたので閉じる
    _put_lid_event(temp_db, created + timedelta(minutes=10), "close")
    assert rp._close_if_premise_gone("anomaly_rice_lid_stuck", created) is True

    # 二度目は閉じる対象が無い（同じ事象で鳴り続けない）
    assert rp._close_if_premise_gone("anomaly_rice_lid_stuck", created) is False
