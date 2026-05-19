"""未対応の pending_notifications の自動タイムアウト処理スクリプト。

ポリシー変更（2026-05-15）:
- LINE 再通知は廃止（深夜帯/翌日にズレた時刻の通知が届く問題のため）
- 未対応通知は **家族管理画面**で一覧表示・対応する方式に変更
- このスクリプトは「24時間以上経過した未対応通知を自動タイムアウト扱い」だけ

旧仕様（参考）: 30分おきに再送 → 最大3回 → タイムアウト
新仕様: 24時間経過した未対応は自動 timeout（家族UI からも消える）。
        新規通知は家族UI の「未対応通知一覧」に常時表示される。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db, get_conn, transaction

TIMEOUT_HOURS = 24  # 24時間以上対応なしの通知は自動タイムアウト扱いに


def main():
    init_db()
    # pending_notifications.last_notified_at / created_at は CURRENT_TIMESTAMP (UTC)
    cutoff = (datetime.utcnow() - timedelta(hours=TIMEOUT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    try:
        timeouts = conn.execute(
            """SELECT id, notification_type, context_key
                 FROM pending_notifications
                WHERE completed_at IS NULL
                  AND created_at < ?""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    if not timeouts:
        print("タイムアウト対象なし")
        return

    for t in timeouts:
        try:
            with transaction() as c:
                c.execute(
                    """UPDATE pending_notifications
                          SET completed_at = CURRENT_TIMESTAMP,
                              completed_by = 'auto_timeout',
                              completed_action = ?
                        WHERE id = ?""",
                    (f"{TIMEOUT_HOURS}時間応答なし → 自動タイムアウト", t["id"]),
                )
            print(f"タイムアウト: id={t['id']} type={t['notification_type']} ctx={t['context_key']}")
        except Exception as e:
            print(f"タイムアウト処理失敗 id={t['id']}: {e}")


if __name__ == "__main__":
    main()
