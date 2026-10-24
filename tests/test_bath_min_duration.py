"""短すぎる「お風呂」をセッションにしないこと。

ドアを閉めて数秒で開ける「通過」が入浴として記録されていた。
2026-10-23 の実測（直近60日129件）: 0.0分が38件(29%)、1分未満が57件(44%)。
家族ダッシュボードと週次レポートの入浴回数が倍近く膨らんでいた。

入浴・湯はり・清掃のいずれも1分未満では成立しないので、そこで切る。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from src.sessions import BATH_MIN_DURATION_SECONDS, aggregate_sessions

# 日中のど真ん中。0時跨ぎで「今日」の判定がぶれないようにする。
NOON = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _add_bath_cluster(db, start: datetime, seconds: float, person_id: int = 1) -> None:
    """お風呂関連イベントだけのクラスタを1本入れる。

    入浴中は脱衣所のモーションが繰り返し出る。間を空けると
    クラスタリングの gap で分割され、bath_start と bath_end が
    別クラスタになってしまうので、30秒おきにモーションを入れる。
    """
    end = start + timedelta(seconds=seconds)
    rows = [
        ("bath_door", "close", start),
        ("bath_door", "bath_start", start),
    ]
    t = 0.0
    while t < seconds:
        rows.append(("bath_motion", "motion", start + timedelta(seconds=t)))
        t += 30
    rows += [
        ("bath_door", "bath_end", end),
        ("bath_door", "open", end),
    ]
    conn = sqlite3.connect(db)
    try:
        for source, ev, at in rows:
            conn.execute(
                "INSERT INTO events(person_id, source, event_type, started_at) "
                "VALUES(?, ?, ?, ?)",
                (person_id, source, ev, _fmt(at)),
            )
        conn.commit()
    finally:
        conn.close()


def _bath_sessions(db) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT started_at, ended_at FROM meal_sessions WHERE label='お風呂' "
            "ORDER BY started_at"
        ).fetchall()
    finally:
        conn.close()


def test_数秒の通過は入浴にしない(seeded_db):
    """ドアを閉めて5秒で開けただけ。これが最も多かった形。"""
    _add_bath_cluster(seeded_db, NOON, seconds=5)

    aggregate_sessions()

    assert _bath_sessions(seeded_db) == []


def test_同時刻のイベントだけなら入浴にしない(seeded_db):
    """実データで最多だった 0.0分（全部同じ秒）のケース。"""
    _add_bath_cluster(seeded_db, NOON, seconds=0)

    aggregate_sessions()

    assert _bath_sessions(seeded_db) == []


def test_十分な長さの入浴は記録する(seeded_db):
    """本物の入浴を落とさないこと。ここが壊れたら安全監視が消える。

    長さは SESSION_GAP_MINUTES(15分) 未満にする。それを超えるとイベントが
    別クラスタに分かれ、この関数が見たい「長さの判定」ではなく
    クラスタリングの挙動を試すことになる。実データでは途中のドア開閉が
    クラスタを繋いでおり、41.5分の入浴も1本として記録されている。"""
    _add_bath_cluster(seeded_db, NOON, seconds=10 * 60)

    aggregate_sessions()

    rows = _bath_sessions(seeded_db)
    assert len(rows) == 1, rows


def test_しきい値ちょうどは記録する(seeded_db):
    """境界を固定する。60秒ちょうどは入浴として扱う。"""
    _add_bath_cluster(seeded_db, NOON, seconds=BATH_MIN_DURATION_SECONDS)

    aggregate_sessions()

    assert len(_bath_sessions(seeded_db)) == 1


def test_しきい値の1秒手前は記録しない(seeded_db):
    _add_bath_cluster(seeded_db, NOON, seconds=BATH_MIN_DURATION_SECONDS - 1)

    aggregate_sessions()

    assert _bath_sessions(seeded_db) == []


def test_短いものを捨てても長いものは残る(seeded_db):
    """同じ日に通過と本物が混ざっていても、本物だけが残ること。"""
    _add_bath_cluster(seeded_db, NOON - timedelta(hours=3), seconds=3)
    _add_bath_cluster(seeded_db, NOON, seconds=10 * 60)

    aggregate_sessions()

    rows = _bath_sessions(seeded_db)
    assert len(rows) == 1, rows
    assert rows[0][0].startswith(NOON.strftime("%Y-%m-%d %H:%M"))


def test_しきい値は現実的な範囲にある():
    """短くしすぎると通過が通り、長くしすぎると短い入浴を落とす。"""
    assert 30 <= BATH_MIN_DURATION_SECONDS <= 300
