"""未対応のpending_notificationsを再通知するスクリプト。

cronで定期実行（5〜15分おき推奨）。
- 完了していないレコード
- 最後の通知から RENOTIFY_INTERVAL_MINUTES 以上経過
- 通知回数が MAX_NOTIFY_COUNT 未満
これらに対して同じQuick Replyで再通知を送る。
"""
import json as _json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db, get_conn, transaction
from src.notifier import (
    broadcast_with_quick_reply, broadcast_line_message,
    send_line_message, send_line_with_quick_reply,
    is_critical_category, _admin_user_id,
)
from src.settings import get_bool

RENOTIFY_INTERVAL_MINUTES = 30
MAX_NOTIFY_COUNT = 3  # 初回 + 再通知2回 = 計3回


def main():
    init_db()
    if not get_bool("notify_master_enabled", default=True):
        print("LINE通知マスタースイッチOFF → 再通知スキップ")
        return

    # pending_notifications.last_notified_at は CURRENT_TIMESTAMP (UTC) で保存されるため
    # cutoff も UTC で計算する。datetime.now() を使うとタイムゾーン分のズレで
    # 30分間隔の制約が無視され、5分おきに再通知が連発する原因になっていた。
    cutoff = (datetime.utcnow() - timedelta(minutes=RENOTIFY_INTERVAL_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, notification_type, context_key, message, quick_reply_json, notify_count
                 FROM pending_notifications
                WHERE completed_at IS NULL
                  AND last_notified_at < ?
                  AND notify_count < ?
                ORDER BY last_notified_at""",
            (cutoff, MAX_NOTIFY_COUNT),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("再通知対象なし")
        return

    for r in rows:
        nid = r["id"]
        msg = "🔁 再通知（" + str(r["notify_count"] + 1) + "回目）\n\n" + r["message"]
        items = []
        if r["quick_reply_json"]:
            try:
                items = _json.loads(r["quick_reply_json"])
            except Exception:
                items = []
        try:
            if is_critical_category(r["notification_type"]):
                # CRITICAL → 全員に再通知
                if items:
                    sent = broadcast_with_quick_reply(msg, items)
                else:
                    sent = broadcast_line_message(msg)
            else:
                # NORMAL → admin のみ
                admin = _admin_user_id()
                if not admin:
                    sent = 0
                elif items:
                    sent = 1 if send_line_with_quick_reply(msg, items, user_id=admin) else 0
                else:
                    sent = 1 if send_line_message(msg, user_id=admin) else 0
            if sent > 0:
                with transaction() as c:
                    c.execute(
                        """UPDATE pending_notifications
                              SET last_notified_at = CURRENT_TIMESTAMP,
                                  notify_count = notify_count + 1
                            WHERE id = ?""",
                        (nid,),
                    )
                print(f"再通知: id={nid} type={r['notification_type']} ctx={r['context_key']} 送信={sent}")
        except Exception as e:
            print(f"再通知失敗 id={nid}: {e}")

    # 最大回数に達したものは「タイムアウトで諦め」を全員に伝える
    conn = get_conn()
    try:
        timeouts = conn.execute(
            """SELECT id, notification_type, context_key, message
                 FROM pending_notifications
                WHERE completed_at IS NULL
                  AND notify_count >= ?
                  AND last_notified_at < ?
                  AND completed_action IS NULL""",
            (MAX_NOTIFY_COUNT, cutoff),
        ).fetchall()
    finally:
        conn.close()

    for t in timeouts:
        try:
            timeout_msg = (
                f"⏰ {RENOTIFY_INTERVAL_MINUTES * MAX_NOTIFY_COUNT}分以上応答なし。\n"
                f"以下の通知は対応されませんでした:\n\n{t['message'][:120]}\n\n"
                "後ほど家族管理画面で確認してください。"
            )
            if is_critical_category(t['notification_type']):
                broadcast_line_message(timeout_msg)
            else:
                admin = _admin_user_id()
                if admin:
                    send_line_message(timeout_msg, user_id=admin)
            with transaction() as c:
                c.execute(
                    """UPDATE pending_notifications
                          SET completed_action = 'timeout'
                        WHERE id = ?""",
                    (t["id"],),
                )
            print(f"タイムアウト通知: id={t['id']}")
        except Exception as e:
            print(f"タイムアウト通知失敗 id={t['id']}: {e}")


if __name__ == "__main__":
    main()
