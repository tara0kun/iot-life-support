"""3ヶ月以上前の events を月次サマリに圧縮 → 物理削除 → VACUUM。

手動実行式（cronに入れない）。バックアップが取れていることを必ず確認してから実行。

Usage:
    venv/bin/python scripts/prune_old_events.py --dry-run   # 確認のみ
    venv/bin/python scripts/prune_old_events.py --execute   # 実行

サマリ化されたデータは events_monthly_summary テーブルに保存:
    (year_month TEXT, source TEXT, event_type TEXT, person_id INTEGER, cnt INTEGER, avg_value REAL)

person_id ごと・event_type ごとの月次集計のみ残るため、長期トレンド分析は維持。
個別イベントの詳細（時刻、value）は失われる。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import get_conn

CUTOFF_MONTHS = 3
SUMMARY_TABLE = "events_monthly_summary"


def ensure_summary_table(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
            year_month TEXT NOT NULL,
            source TEXT NOT NULL,
            event_type TEXT,
            person_id INTEGER,
            cnt INTEGER NOT NULL,
            avg_value REAL,
            PRIMARY KEY (year_month, source, event_type, person_id)
        )
    """)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="削除せず影響範囲のみ表示")
    ap.add_argument("--execute", action="store_true", help="実際に剪定実行")
    ap.add_argument("--months", type=int, default=CUTOFF_MONTHS,
                    help=f"何ヶ月以上前を対象とするか (default {CUTOFF_MONTHS})")
    args = ap.parse_args()

    if not (args.dry_run or args.execute):
        ap.error("--dry-run か --execute のいずれかを指定")

    with get_conn() as conn:
        cutoff = conn.execute(
            f"SELECT date('now', '-{args.months} months', 'localtime') AS d"
        ).fetchone()["d"]

        # 対象範囲の集計
        target = conn.execute(
            "SELECT COUNT(*) AS cnt, MIN(started_at) AS first, MAX(started_at) AS last "
            "FROM events WHERE started_at < ?",
            (cutoff,),
        ).fetchone()
        print(f"剪定対象: started_at < {cutoff}")
        print(f"  件数: {target['cnt']:,}")
        print(f"  期間: {target['first']} 〜 {target['last']}")

        if target["cnt"] == 0:
            print("剪定対象なし。終了。")
            return

        # サマリ化後の行数見込
        summary_rows = conn.execute("""
            SELECT COUNT(*) AS cnt FROM (
                SELECT strftime('%Y-%m', started_at) ym, source, event_type, person_id
                FROM events WHERE started_at < ?
                GROUP BY ym, source, event_type, person_id
            )
        """, (cutoff,)).fetchone()["cnt"]
        print(f"  サマリ化後の行数: {summary_rows:,} ({target['cnt']/summary_rows:.0f}x 圧縮)")

        if args.dry_run:
            print("\n--dry-run のため変更なし。実行するには --execute")
            return

        # バックアップ存在チェック
        backup_dir = ROOT / "data" / "backup"
        latest = sorted(backup_dir.glob("iot_*.db"))[-1:] if backup_dir.exists() else []
        if not latest:
            print("⚠️ data/backup/ にバックアップが見当たりません。中止")
            return
        print(f"  最新バックアップ: {latest[0].name}")

        # サマリ表作成 + 集計挿入
        ensure_summary_table(conn)
        conn.execute(f"""
            INSERT OR REPLACE INTO {SUMMARY_TABLE} (year_month, source, event_type, person_id, cnt, avg_value)
            SELECT strftime('%Y-%m', started_at), source, event_type, person_id,
                   COUNT(*), AVG(value)
              FROM events WHERE started_at < ?
             GROUP BY 1, 2, 3, 4
        """, (cutoff,))
        # 物理削除
        cur = conn.execute("DELETE FROM events WHERE started_at < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        print(f"✅ events から {deleted:,} 行削除、summary {summary_rows:,} 行作成")

    # VACUUM は別接続 (トランザクション外)
    import sqlite3
    with sqlite3.connect(ROOT / "data" / "iot.db") as c:
        print("VACUUM 中... (数十秒かかる)")
        c.execute("VACUUM")
    print("✅ VACUUM 完了")


if __name__ == "__main__":
    main()
