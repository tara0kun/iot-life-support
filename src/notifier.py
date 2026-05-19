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


# LINE Messaging API の月200通制限対策。
# 重要度CRITICALのカテゴリだけ全家族にbroadcast、それ以外は admin（LINE_USER_ID=孫）のみ。
CRITICAL_CATEGORIES: set[str] = {
    "bath_emergency",        # 浴室30分無反応（緊急）
    "anomaly_inactivity",    # 4時間センサー無反応
    "anomaly_night_rice",    # 深夜炊飯
    "anomaly_fridge_open",   # 冷蔵庫開きっぱなし
    "meal_alert",            # 食べすぎアラート
    "device_locked",         # 自動ロック / 手動ロック
    "long_toilet_stay",      # トイレに長時間（5分以上）滞在
}

# 深夜帯（1〜5時）でも通知を許可するカテゴリ（トイレ・緊急のみ）
NIGHT_ALLOWED_CATEGORIES: set[str] = {
    "bath_emergency",        # 緊急: 浴室で動きなし
    "anomaly_inactivity",    # 緊急: センサー無反応
    "anomaly_night_rice",    # 深夜炊飯は本来通知すべき異常
    "long_toilet_stay",      # トイレ長時間滞在は深夜でも通知（転倒等のリスク）
}

NIGHT_QUIET_START_HOUR = 1   # 1:00〜
NIGHT_QUIET_END_HOUR = 5     # 5:00 までの通知を抑制


def _is_night_quiet_hours() -> bool:
    """現在時刻が夜間通知抑制時間帯（1:00〜5:00）か判定。"""
    h = datetime.now().hour
    return NIGHT_QUIET_START_HOUR <= h < NIGHT_QUIET_END_HOUR


def _should_suppress_for_night(category: str) -> bool:
    """深夜帯で抑制対象か。緊急系・トイレ関連は通す。"""
    if not _is_night_quiet_hours():
        return False
    if category in NIGHT_ALLOWED_CATEGORIES:
        return False
    return True


def is_critical_category(category: str) -> bool:
    return category in CRITICAL_CATEGORIES


def _admin_user_id() -> str:
    """孫=LINE_USER_ID（adminアカウント）"""
    return _load_env().get("LINE_USER_ID", "").strip()


def send_line_message(message: str, user_id: str | None = None) -> bool:
    """LINEプッシュ通知を送る。

    - user_id 指定なし → admin (LINE_USER_ID=孫) のみに送信（コスト最小）
    - user_id 指定あり → そのユーザーのみに送信
    全家族にbroadcastしたい場合は明示的に broadcast_line_message を呼ぶ。
    """
    # マスタースイッチ確認
    try:
        from .settings import get_bool
        if not get_bool("notify_master_enabled", default=True):
            log.info("LINE通知マスタースイッチOFF → 送信スキップ: %s", message[:50])
            return False
    except Exception:
        pass

    # user_id 未指定 → admin のみ（broadcast したいなら明示呼出）
    if user_id is None:
        user_id = _admin_user_id()
        if not user_id:
            log.warning("LINE_USER_ID 未設定")
            return False

    env = _load_env()
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token or not user_id:
        log.warning("LINE設定未完了（トークンまたはユーザーIDなし）")
        return False

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = {
        "to": user_id,
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
    """通知先LINE user_id 一覧。.env (LINE_ALLOWED_SENDERS / LINE_USER_ID) と
    DBの family_line_users テーブルの両方を統合（重複排除）。
    """
    ids: list[str] = []
    seen: set[str] = set()

    def add(uid: str) -> None:
        uid = (uid or "").strip()
        if uid and uid not in seen:
            seen.add(uid)
            ids.append(uid)

    env = _load_env()
    for s in env.get("LINE_ALLOWED_SENDERS", "").split(","):
        add(s)
    add(env.get("LINE_USER_ID", ""))

    # DB登録家族も追加
    try:
        from .db import get_conn
        conn = get_conn()
        try:
            for r in conn.execute("SELECT line_user_id FROM family_line_users").fetchall():
                add(r["line_user_id"])
        finally:
            conn.close()
    except Exception as e:
        log.warning("family_line_users 読み込み失敗: %s", e)
    return ids


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
        if it.get("uri"):
            # URLボタン（家族UIやガイドへの遷移）
            items.append({
                "type": "action",
                "action": {
                    "type": "uri",
                    "label": it["label"][:20],
                    "uri": it["uri"],
                },
            })
        elif it.get("data"):
            # postback（ボタン押下時にサーバ側で処理）
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


def send_line_image(image_url: str, preview_url: str | None = None,
                     caption: str | None = None, user_id: str | None = None) -> bool:
    """LINE プッシュで画像を1人に送信。

    image_url, preview_url は HTTPS の公開URL（LINEサーバーが取得しに来る）。
    caption は画像と一緒に送るテキスト（指定時）。
    user_id 未指定なら admin (LINE_USER_ID)。
    """
    try:
        from .settings import get_bool
        if not get_bool("notify_master_enabled", default=True):
            log.info("LINE通知マスタースイッチOFF → 画像送信スキップ")
            return False
    except Exception:
        pass

    env = _load_env()
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    uid = (user_id or _admin_user_id()).strip()
    if not token or not uid:
        log.warning("LINE 画像送信失敗（トークン or user_id なし）")
        return False

    messages: list[dict] = []
    if caption:
        messages.append({"type": "text", "text": caption})
    messages.append({
        "type": "image",
        "originalContentUrl": image_url,
        "previewImageUrl": preview_url or image_url,
    })

    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            json={"to": uid, "messages": messages},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("LINE画像送信OK: %s", image_url[:80])
            return True
        log.warning("LINE画像送信失敗: %d %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.error("LINE画像送信エラー: %s", e)
        return False


def broadcast_line_image(image_url: str, preview_url: str | None = None,
                         caption: str | None = None) -> int:
    """LINE_ALLOWED_SENDERS + family_line_users 全員に画像を送信。戻り値=成功数。"""
    recipients = _load_recipients()
    if not recipients:
        return 0
    sent = 0
    for uid in recipients:
        if send_line_image(image_url, preview_url, caption, user_id=uid):
            sent += 1
    return sent


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

def send_actionable_notification(category: str, context_key: str, message: str,
                                  extra_items: list[dict] | None = None,
                                  default_choices: bool = True) -> int:
    """確認ボタン付きで全家族にbroadcast、pending_notificationsに記録する。

    デフォルトの選択肢（default_choices=True）:
      - 「了解（対応します）」: 対応済みとしてマーク
      - 「対応不要」: 確認したが対応はしない（誤検知扱い、学習対象外）
    extra_items でこれをオーバーライドすることも可能（例: bath_classification は完全カスタム）。

    家族の誰かが選択肢を押すと:
      - pending_notifications.completed_at が記録される
      - 全家族に「☑️ 対応済み」がbroadcastされる
      - recheck_pending.pyによる再通知が止まる

    誰も応答しない場合は recheck_pending.py が30分間隔で最大2回再通知し、
    最終的に「⏰ 応答なし」をbroadcastして諦める。

    引数:
      category: 通知の種類識別子（例 "meal_alert", "anomaly_night_rice"）
      context_key: 同一文脈を一意に識別するキー（例 "2026-05-02_meal_3"）
      message: 通知文
      extra_items: 追加したいQuick Replyボタン
      default_choices: True なら「了解」「対応不要」の標準2択を入れる。False なら extra_items のみ
    """
    # 深夜帯（1〜5時）の通知抑制（緊急系・深夜異常検知のみ通す）
    if _should_suppress_for_night(category):
        log.info("[notify] 深夜帯のため抑制: category=%s", category)
        # pending には記録しない（再通知ループに乗らないように）
        return 0

    items: list[dict] = []
    if default_choices:
        items = [
            {"label": "✓ 了解（対応します）", "data": f"confirm:{category}:{context_key}"},
            {"label": "対応不要（誤検知）", "data": f"confirm_dismiss:{category}:{context_key}"},
        ]
    if extra_items:
        items.extend(extra_items)
    items = items[:13]  # LINE Quick Reply は最大13個

    # カテゴリで送信先振り分け（CRITICAL=全員、その他=adminのみ）
    if is_critical_category(category):
        sent = broadcast_with_quick_reply(message, items)
    else:
        admin = _admin_user_id()
        if admin and send_line_with_quick_reply(message, items, user_id=admin):
            sent = 1
        else:
            sent = 0
    if sent > 0:
        record_pending_notification(category, context_key, message, items)
    return sent

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


def resolve_confirmer_name(line_user_id: str) -> str:
    """LINE user_id から家族登録名（祖母/母/etc）を解決する。

    family_line_users テーブルに登録があればその person.name を返す。
    なければ「誰か」（未登録）を返す。
    """
    if not line_user_id:
        return "誰か"
    try:
        from .db import get_conn
        conn = get_conn()
        try:
            row = conn.execute(
                """SELECT p.name FROM family_line_users f
                   LEFT JOIN persons p ON p.id = f.person_id
                   WHERE f.line_user_id = ?""",
                (line_user_id,),
            ).fetchone()
            if row and row["name"]:
                return row["name"]
        finally:
            conn.close()
    except Exception as e:
        log.warning("confirmer_name 解決失敗: %s", e)
    return "誰か"


def mark_related_completed_silent(notification_type: str, context_prefix: str,
                                   completed_by: str, action_summary: str) -> int:
    """同一日の関連通知を「サイレントに」完了マークする（broadcastしない）。

    用途: device_locked と meal_alert のように、同じイベントから派生した複数通知を
    片方の確認で全部閉じる。LINEへの追加メッセージ送信は行わない。
    戻り値: 完了マークした件数
    """
    from .db import transaction
    try:
        with transaction() as conn:
            cur = conn.execute(
                """UPDATE pending_notifications
                      SET completed_at = CURRENT_TIMESTAMP,
                          completed_by = ?, completed_action = ?
                    WHERE notification_type = ?
                      AND context_key LIKE ?
                      AND completed_at IS NULL""",
                (completed_by[:64], action_summary, notification_type, f"{context_prefix}%"),
            )
            return cur.rowcount or 0
    except Exception as e:
        log.warning("関連通知の自動完了に失敗: %s", e)
        return 0


def mark_notification_completed(notification_type: str, context_key: str,
                                 completed_by: str, action_summary: str) -> bool:
    """未完了の通知を完了マーク + 全家族にブロードキャスト通知。

    対応者の名前を family_line_users から解決し、broadcastの先頭に表示する。
    既に完了済みなら何もしない（重複実行ガード）。
    """
    from .db import get_conn, transaction
    confirmer = resolve_confirmer_name(completed_by)
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

        # 「誰が何を確認/対応したか」は家族間で共有すべき情報なので常に全員にbroadcast
        # （誰が対応中か把握できないと家族同士で重複対応するため）
        msg = f"☑️ {confirmer}さんが対応しました\n{action_summary}"
        broadcast_line_message(msg)
        return True
    except Exception as e:
        log.error("pending_notification 完了処理失敗: %s", e)
        return False


def update_webhook_url(url: str, max_retries: int = 5, retry_interval_sec: int = 6) -> bool:
    """LINE Messaging APIのwebhook URLを更新する（Cloudflare Tunnel再起動時用）。

    Cloudflare Tunnel 発行直後は DNS が伝播していないことがあり、
    LINE 側が「Invalid webhook endpoint URL」を返すケースがあるため、
    リトライしながら更新を試みる。
    """
    import time as _time
    env = _load_env()
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        log.warning("LINE webhook更新失敗（トークンなし）")
        return False
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.put(
                "https://api.line.me/v2/bot/channel/webhook/endpoint",
                headers=headers,
                json={"endpoint": url},
                timeout=10,
            )
            if resp.status_code == 200:
                log.info("LINE webhook URL更新成功 (attempt=%d): %s", attempt, url)
                return True
            log.warning(
                "LINE webhook更新失敗 (attempt=%d/%d): %d %s",
                attempt, max_retries, resp.status_code, resp.text[:200],
            )
        except Exception as e:
            log.error("LINE webhook更新エラー (attempt=%d/%d): %s", attempt, max_retries, e)
        if attempt < max_retries:
            _time.sleep(retry_interval_sec)
    return False


def notify_meal_alert(person_name: str, meal_count: int, last_meal_time: str) -> bool:
    now = datetime.now()
    message = (
        f"🍚 {person_name}さんが食事行動を検知しました\n"
        f"本日{meal_count}回目（前回: {last_meal_time}）\n"
        f"検知時刻: {now.strftime('%H:%M')}\n"
        f"\nさりげなく声をかけてあげてください"
    )
    today = now.strftime("%Y-%m-%d")
    return send_actionable_notification(
        "meal_alert", f"{today}_{meal_count}", message
    ) > 0


_DEVICE_LABELS = {
    "rice_cooker": "炊飯器",
    "ih": "IHコンロ",
}


def notify_device_locked(device_name: str, manual: bool = False, reason: str = "") -> bool:
    """機器ロック時の家族全員への情報通知（追加確認は不要）。

    manual=True なら家族の手動操作。
    自動ロックは現在は使われていない（_request_lock_confirmation で事前確認するため）。
    家族間の重複対応を防ぐためシンプル broadcast のみ。
    """
    label = _DEVICE_LABELS.get(device_name, device_name)
    now = datetime.now()
    if manual:
        sub = f"理由: {reason}" if reason else "家族による手動操作"
        message = (
            f"🔒 {label}を手動ロックしました\n"
            f"{sub}\n"
            f"時刻: {now.strftime('%H:%M')}\n"
            "（解除するには家族管理画面 or LINEに「ロック解除」と送信）"
        )
    else:
        message = (
            f"🔒 {label}をロックしました\n"
            f"時刻: {now.strftime('%H:%M')}\n"
            "（解除するには家族管理画面 or LINEに「ロック解除」と送信）"
        )
    return broadcast_line_message(message) > 0


def notify_device_unlocked(device_name: str, manual: bool = False, reason: str = "") -> bool:
    """機器ロック解除時の通知。家族の誰が解除したかを全員に共有する。

    手動解除（家族の操作）は全員にbroadcast（重複操作防止のため）。
    自動解除は admin のみ（情報通知）。
    """
    label = _DEVICE_LABELS.get(device_name, device_name)
    now = datetime.now()
    if manual:
        sub = f"理由: {reason}" if reason else "家族による手動解除"
        message = f"🔓 {label}のロックを家族が手動で解除しました\n{sub}\n時刻: {now.strftime('%H:%M')}"
        return broadcast_line_message(message) > 0
    else:
        message = f"🔓 {label}のロックが自動で解除されました\n時刻: {now.strftime('%H:%M')}"
        return send_line_message(message)  # 自動解除は情報通知 → admin のみ
