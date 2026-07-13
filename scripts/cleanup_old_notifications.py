"""古い pending_notifications と session_events を圧縮する定期メンテ。

pending_notifications:
  - 90日以上前の完了済レコードは物理削除 (DB backup があるので復元可能)
  - 30日以上前の未応答レコードは "expired" マークをつけてから 90日で削除

session_events:
  - 90日以上前は物理削除 (集約済セッションは meal_sessions.event_count に残るため統計への影響なし)

events は prune_old_events.py が別途 3ヶ月 → 月次サマリ集計する仕組みを用意済 (scripts/prune_old_events.py)。
このスクリプトは通知系のみ担当する。

Usage:
    venv/bin/python scripts/cleanup_old_notifications.py                # 実行
    venv/bin/python scripts/cleanup_old_notifications.py --dry-run      # 影響件数のみ確認

cron: 0 3 * * 0  (毎週日曜 03:00、DB バックアップ cron と衝突しない時間帯)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import get_conn

PENDING_COMPLETE_DAYS = 90       # 完了済はこれより古いなら削除
PENDING_ORPHAN_DAYS = 30         # 未応答をこれ以上放置したら "expired" マーク
SESSION_EVENT_DAYS = 90          # session_events は3ヶ月保持
BATHROOM_READING_DAYS = 7        # bathroom_meter/reading は 7日で TTL (入浴判定に使う直近分のみ)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="削除せず件数のみ表示")
    args = ap.parse_args()

    with get_conn() as conn:
        # 1) 30日以上放置の未応答通知に "expired" マーク
        cur = conn.execute(f"""
            SELECT COUNT(*) FROM pending_notifications
             WHERE completed_at IS NULL
               AND created_at < datetime('now', '-{PENDING_ORPHAN_DAYS} days')
        """)
        n_expiring = cur.fetchone()[0]

        # 2) 90日以上前の完了済 (今回 expired にするものも含む) を物理削除
        cur = conn.execute(f"""
            SELECT COUNT(*) FROM pending_notifications
             WHERE completed_at IS NOT NULL
               AND completed_at < datetime('now', '-{PENDING_COMPLETE_DAYS} days')
        """)
        n_delete_notif = cur.fetchone()[0]

        # 3) session_events (session_id, event_id のみ、時刻は events 側で判定)
        cur = conn.execute(f"""
            SELECT COUNT(*) FROM session_events
             WHERE event_id IN (
                 SELECT id FROM events
                  WHERE started_at < datetime('now', '-{SESSION_EVENT_DAYS} days', 'localtime')
             )
        """)
        n_delete_sev = cur.fetchone()[0]

        # 4) bathroom_meter/reading (10秒毎の温湿度、直近分のみ入浴判定に使用)
        cur = conn.execute(f"""
            SELECT COUNT(*) FROM events
             WHERE source='bathroom_meter' AND event_type='reading'
               AND started_at < datetime('now', '-{BATHROOM_READING_DAYS} days', 'localtime')
        """)
        n_delete_reading = cur.fetchone()[0]

        print(f"pending_notifications:")
        print(f"  {PENDING_ORPHAN_DAYS}日超放置 → expired マーク : {n_expiring}件")
        print(f"  {PENDING_COMPLETE_DAYS}日超前の完了済 → 削除    : {n_delete_notif}件")
        print(f"session_events {SESSION_EVENT_DAYS}日超 → 削除 : {n_delete_sev:,}件")
        print(f"bathroom_meter/reading {BATHROOM_READING_DAYS}日超 → 削除 : {n_delete_reading:,}件")

        if args.dry_run:
            print("\n[dry-run] 変更なし。実行するには --dry-run を外す")
            return

        # expired マーク (completed_at は他の全 UPDATE と揃えて UTC で保存)
        conn.execute(f"""
            UPDATE pending_notifications
               SET completed_at = CURRENT_TIMESTAMP,
                   completed_by = 'auto_expired',
                   completed_action = 'expired'
             WHERE completed_at IS NULL
               AND created_at < datetime('now', '-{PENDING_ORPHAN_DAYS} days')
        """)
        # 削除
        conn.execute(f"""
            DELETE FROM pending_notifications
             WHERE completed_at IS NOT NULL
               AND completed_at < datetime('now', '-{PENDING_COMPLETE_DAYS} days')
        """)
        conn.execute(f"""
            DELETE FROM session_events
             WHERE event_id IN (
                 SELECT id FROM events
                  WHERE started_at < datetime('now', '-{SESSION_EVENT_DAYS} days', 'localtime')
             )
        """)
        conn.commit()

        # bathroom_meter/reading は件数が多い (35万件) ため chunk 削除 (iot-monitor との書込競合を避ける)
        chunk = 5000
        total_deleted = 0
        while True:
            cur = conn.execute(f"""
                DELETE FROM events
                 WHERE rowid IN (
                     SELECT rowid FROM events
                      WHERE source='bathroom_meter' AND event_type='reading'
                        AND started_at < datetime('now', '-{BATHROOM_READING_DAYS} days', 'localtime')
                      LIMIT {chunk}
                 )
            """)
            n = cur.rowcount
            conn.commit()
            total_deleted += n
            if n == 0:
                break
        print(f"\n✅ bathroom_meter/reading chunk 削除 {total_deleted:,} 件完了")
        print("✅ 削除・マーク完了")


if __name__ == "__main__":
    main()
