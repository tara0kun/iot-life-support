"""古いイベントデータをアーカイブする。

90日以上前のイベントをeventsテーブルから削除し、
アーカイブDBに移動する。daily_scoresは保持する。

crontab例:
  0 4 1 * *  cd ~/IoT && venv/bin/python scripts/archive_old_data.py
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import DB_PATH, get_conn, transaction

ARCHIVE_DB = DB_PATH.parent / "archive" / f"events_{datetime.now().strftime('%Y%m')}.db"
RETENTION_DAYS = 90


def archive():
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
    ARCHIVE_DB.parent.mkdir(exist_ok=True)

    # アーカイブDBにテーブル作成
    arch = sqlite3.connect(ARCHIVE_DB)
    arch.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER, person_id INTEGER, source TEXT, event_type TEXT,
        started_at TEXT, ended_at TEXT, value REAL, confidence REAL, raw_meta TEXT
    )""")

    conn = get_conn()
    try:
        old_events = conn.execute(
            "SELECT id, person_id, source, event_type, started_at, ended_at, value, confidence, raw_meta FROM events WHERE started_at < ?",
            (cutoff,),
        ).fetchall()

        if not old_events:
            print(f"アーカイブ対象なし（{RETENTION_DAYS}日以上前のイベントがありません）")
            return

        # アーカイブDBに挿入
        arch.executemany(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
            [(r["id"], r["person_id"], r["source"], r["event_type"],
              r["started_at"], r["ended_at"], r["value"], r["confidence"], r["raw_meta"])
             for r in old_events],
        )
        arch.commit()

        # 本体DBから削除
        with transaction() as tx:
            tx.execute("DELETE FROM session_events WHERE event_id IN (SELECT id FROM events WHERE started_at < ?)", (cutoff,))
            tx.execute("DELETE FROM events WHERE started_at < ?", (cutoff,))

        print(f"アーカイブ完了: {len(old_events)}件 → {ARCHIVE_DB}")
    finally:
        conn.close()
        arch.close()


if __name__ == "__main__":
    archive()
