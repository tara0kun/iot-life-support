"""LINE通知モジュール。

食事行動の2回目検知時に家族へLINE通知を送る。
LINE Messaging API (Push Message) を使用。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger("notifier")


def _load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def send_line_message(message: str, user_id: str | None = None) -> bool:
    # マスタースイッチ確認（settings.notify_master_enabled）
    # importは関数内（循環import回避＋settings未初期化時の安全のため）
    try:
        from .settings import get_bool
        if not get_bool("notify_master_enabled", default=True):
            log.info("LINE通知マスタースイッチOFF → 送信スキップ: %s", message[:50])
            return False
    except Exception:
        pass  # settingsが使えない状態でも通知は送る（フェイルオープン）

    env = _load_env()
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    uid = user_id or env.get("LINE_USER_ID", "")

    if not token or not uid:
        log.warning("LINE設定未完了（トークンまたはユーザーIDなし）")
        return False

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = {
        "to": uid,
        "messages": [{"type": "text", "text": message}],
    }

    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json=data,
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("LINE通知送信: %s", message[:50])
            return True
        else:
            log.warning("LINE通知失敗: %d %s", resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        log.error("LINE通知エラー: %s", e)
        return False


def _load_recipients() -> list[str]:
    """通知先LINE user_id 一覧。LINE_ALLOWED_SENDERS優先、なければLINE_USER_ID。"""
    env = _load_env()
    senders = env.get("LINE_ALLOWED_SENDERS", "").strip()
    if senders:
        ids = [s.strip() for s in senders.split(",") if s.strip()]
        if ids:
            return ids
    fallback = env.get("LINE_USER_ID", "").strip()
    return [fallback] if fallback else []


def broadcast_line_message(message: str) -> int:
    """登録済みの全家族LINE宛先にプッシュ通知。戻り値=送信成功数。

    マスタースイッチがOFFなら何もしない。
    """
    try:
        from .settings import get_bool
        if not get_bool("notify_master_enabled", default=True):
            log.info("LINE通知マスタースイッチOFF → broadcast送信スキップ")
            return 0
    except Exception:
        pass

    recipients = _load_recipients()
    if not recipients:
        log.warning("LINE通知先が設定されていません")
        return 0
    sent = 0
    for uid in recipients:
        if send_line_message(message, user_id=uid):
            sent += 1
    return sent


def broadcast_with_quick_reply(message: str, quick_items: list[dict]) -> int:
    """登録済みの全家族LINE宛先にQuick Reply付きでプッシュ通知。戻り値=成功数。"""
    recipients = _load_recipients()
    if not recipients:
        return 0
    sent = 0
    for uid in recipients:
        if send_line_with_quick_reply(message, quick_items, user_id=uid):
            sent += 1
    return sent


def send_line_with_quick_reply(message: str, quick_items: list[dict], user_id: str | None = None) -> bool:
    """Quick Reply 付きメッセージを送信する。

    quick_items: [{"label": "祖母", "data": "attribute:S:1"}, ...]
        label: ボタンに表示するテキスト（最大20文字）
        data: postback として送信されるデータ文字列
    """
    try:
        from .settings import get_bool
        if not get_bool("notify_master_enabled", default=True):
            log.info("LINE通知マスタースイッチOFF → Quick Reply送信スキップ")
            return False
    except Exception:
        pass

    env = _load_env()
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    uid = user_id or env.get("LINE_USER_ID", "")
    if not token or not uid:
        log.warning("LINE設定未完了 (Quick Reply)")
        return False

    items = []
    for it in quick_items[:13]:  # LINEは最大13個まで
        items.append({
            "type": "action",
            "action": {
                "type": "postback",
                "label": it["label"][:20],
                "data": it["data"],
                "displayText": it.get("display_text", it["label"]),
            },
        })

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = {
        "to": uid,
        "messages": [{
            "type": "text",
            "text": message,
            "quickReply": {"items": items},
        }],
    }
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json=data,
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("LINE Quick Reply送信: %s", message[:50])
            return True
        log.warning("LINE Quick Reply失敗: %d %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.error("LINE Quick Replyエラー: %s", e)
        return False


def reply_line_message(reply_token: str, message: str) -> bool:
    """LINE Reply API でメッセージを返信する（webhook応答用）。"""
    env = _load_env()
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token or not reply_token:
        log.warning("LINE返信失敗（トークンまたはreply_tokenなし）")
        return False
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": message}],
    }
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=headers,
            json=data,
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("LINE返信送信: %s", message[:50])
            return True
        log.warning("LINE返信失敗: %d %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.error("LINE返信エラー: %s", e)
        return False


# ============================================================
# Pending notification 管理（応答未済の追跡＋再通知＋完了ブロードキャスト）
# ============================================================

def record_pending_notification(notification_type: str, context_key: str,
                                 message: str, quick_items: list[dict]) -> int | None:
    """ブロードキャスト送信時にDBに記録。戻り値=記録ID。

    既存レコードがあれば更新（last_notified_at, notify_count++）。
    未完了のレコードは recheck cronによって再通知される。
    """
    import json as _json
    from .db import get_conn, transaction
    try:
        with transaction() as conn:
            existing = conn.execute(
                "SELECT id, completed_at FROM pending_notifications WHERE notification_type = ? AND context_key = ?",
                (notification_type, context_key),
            ).fetchone()
            if existing:
                if existing["completed_at"]:
                    return existing["id"]  # 既に完了済みなら何もしない
                conn.execute(
                    """UPDATE pending_notifications
                          SET last_notified_at = CURRENT_TIMESTAMP,
                              notify_count = notify_count + 1,
                              message = ?, quick_reply_json = ?
                        WHERE id = ?""",
                    (message, _json.dumps(quick_items, ensure_ascii=False), existing["id"]),
                )
                return existing["id"]
            cur = conn.execute(
                """INSERT INTO pending_notifications
                       (notification_type, context_key, message, quick_reply_json)
                   VALUES(?, ?, ?, ?)""",
                (notification_type, context_key, message,
                 _json.dumps(quick_items, ensure_ascii=False)),
            )
            return cur.lastrowid
    except Exception as e:
        log.error("pending_notification 記録失敗: %s", e)
        return None


def mark_notification_completed(notification_type: str, context_key: str,
                                 completed_by: str, action_summary: str) -> bool:
    """未完了の通知を完了マーク + 全家族にブロードキャスト通知。

    既に完了済みなら何もしない（重複実行ガード）。
    """
    from .db import get_conn, transaction
    try:
        with transaction() as conn:
            row = conn.execute(
                "SELECT id, completed_at FROM pending_notifications WHERE notification_type = ? AND context_key = ?",
                (notification_type, context_key),
            ).fetchone()
            if not row:
                # pending未登録（古い通知 or テーブル新規導入前）でもブロードキャストはする
                pass
            elif row["completed_at"]:
                # 既に他の家族が対応済み → 競合ガード
                log.info("pending_notification は既に完了済み: %s/%s", notification_type, context_key)
                return False
            else:
                conn.execute(
                    """UPDATE pending_notifications
                          SET completed_at = CURRENT_TIMESTAMP,
                              completed_by = ?, completed_action = ?
                        WHERE id = ?""",
                    (completed_by[:64], action_summary, row["id"]),
                )

        # 全家族に「対応済み」ブロードキャスト
        broadcast_line_message(f"☑️ 対応済み\n{action_summary}")
        return True
    except Exception as e:
        log.error("pending_notification 完了処理失敗: %s", e)
        return False


def update_webhook_url(url: str) -> bool:
    """LINE Messaging APIのwebhook URLを更新する（Cloudflare Tunnel再起動時用）。"""
    env = _load_env()
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        log.warning("LINE webhook更新失敗（トークンなし）")
        return False
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    try:
        resp = requests.put(
            "https://api.line.me/v2/bot/channel/webhook/endpoint",
            headers=headers,
            json={"endpoint": url},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("LINE webhook URL更新成功: %s", url)
            return True
        log.warning("LINE webhook更新失敗: %d %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.error("LINE webhook更新エラー: %s", e)
        return False


def notify_meal_alert(person_name: str, meal_count: int, last_meal_time: str) -> bool:
    now = datetime.now().strftime("%H:%M")
    message = (
        f"🍚 {person_name}さんが食事行動を検知しました\n"
        f"本日{meal_count}回目（前回: {last_meal_time}）\n"
        f"検知時刻: {now}\n"
        f"\nさりげなく声をかけてあげてください"
    )
    return send_line_message(message)


def notify_device_locked(device_name: str) -> bool:
    device_labels = {
        "rice_cooker": "炊飯器",
        "ih": "IHコンロ",
    }
    label = device_labels.get(device_name, device_name)
    message = f"🔒 {label}を自動ロックしました（食事後の安全措置）"
    return send_line_message(message)
