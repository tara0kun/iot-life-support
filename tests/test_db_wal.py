"""journal_mode と、よく引くクエリ形のインデックスを固定する（Issue #16）。

delete モードでは読み手が書き手をブロックする。実測（452,497行のコピー）では、
集計の読み込み中に INSERT が **104ms** 待たされ、WAL では **1ms** だった。
本番では、祖母のタブレットが `database is locked` で 500 を返す障害になっていた
（2026-09-26、10分おきに再発）。

journal_mode は DB ファイルに永続化されるが、古いバックアップから復元すると
delete で戻ってくる。init_db() が毎回これを直す役目を持つので、
その保証をここで固定する。
"""
from __future__ import annotations

import sqlite3

import pytest


def _mode(path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def test_init_db_enables_wal(temp_db):
    """init_db() を通った DB は WAL になっている。"""
    assert _mode(temp_db) == "wal"


def test_init_db_repairs_a_restored_delete_mode_db(temp_db):
    """delete モードに戻された DB も、次の init_db() で WAL に直る。

    バックアップから復元したときにこれが効かないと、復元後だけ
    ロック競合が再発して原因が分からなくなる。
    """
    import src.db as db

    conn = sqlite3.connect(temp_db)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    assert _mode(temp_db) == "delete"

    db.init_db()

    assert _mode(temp_db) == "wal"


def test_source_started_index_exists(temp_db):
    """「あるセンサーの、ある期間」のインデックスがあること。

    死活判定・各異常検知・セッション集約・家族UIの描画がこの形で引く。
    実測で 25ms → 3ms。
    """
    conn = sqlite3.connect(temp_db)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
        )}
    finally:
        conn.close()
    assert "idx_events_source_started" in names, f"既存: {sorted(names)}"


def test_index_is_actually_used_by_the_planner(temp_db):
    """クエリプランナが実際にそのインデックスを選ぶこと。

    索引があっても使われない形で書かれていたら意味がない。
    """
    conn = sqlite3.connect(temp_db)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM events "
            "WHERE source='camera' AND started_at >= '2026-10-01'"
        ).fetchall()
    finally:
        conn.close()
    text = " ".join(str(r) for r in plan)
    assert "idx_events_source_started" in text, text


def test_ensure_wal_survives_a_locked_database(temp_db, monkeypatch):
    """モードを切り替えられなくても例外を外に出さない。

    切り替えには一瞬だけ排他ロックが要る。取れなかったときに起動が
    止まると、監視そのものが上がらなくなる。
    """
    import src.db as db

    class FakeConn:
        def execute(self, sql, *a):
            raise sqlite3.OperationalError("database is locked を模擬")

    db._ensure_wal(FakeConn())  # 例外が出ないこと


def test_backup_script_keeps_permissions_tight():
    """バックアップに chmod 600 が残っていること。

    cron の生 cp を backup_db.sh に差し替えたとき、cron 側でやっていた
    chmod を落とすと、祖母の生活記録が他ユーザーから読めるようになる。
    """
    from pathlib import Path

    sh = Path(__file__).resolve().parent.parent / "scripts" / "backup_db.sh"
    text = sh.read_text(encoding="utf-8")
    assert 'chmod 600 "$DEST"' in text, "backup_db.sh に chmod 600 が無い"


def test_backup_script_does_not_use_raw_cp():
    """生 cp ではなく sqlite3.backup() を使っていること。

    WAL では iot.db 単体の cp が不整合なコピーを作る。
    """
    from pathlib import Path

    sh = Path(__file__).resolve().parent.parent / "scripts" / "backup_db.sh"
    text = sh.read_text(encoding="utf-8")
    assert "src.backup(dst)" in text

    # コメント行を除いた実行行だけを見る（説明文中の cp に反応させない）
    commands = [
        ln.strip() for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    offenders = [ln for ln in commands if ln.startswith("cp ") or " cp " in ln]
    assert not offenders, f"生 cp が残っている: {offenders}"
