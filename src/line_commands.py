"""LINE webhookで受信したメッセージを解釈して応答文字列を返すディスパッチャ。

優先順位: ロック解除 > 確認コード(数字4桁) > 状況 > 最後の食事 > ヘルプ > リンク
確認コードとロックアウト情報はプロセス内メモリに保持（再起動で消える）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime

from .db import get_conn, transaction
from .lock_manager import get_device_state, unlock_device
from .sessions import sessions_today, merge_sessions_manual

log = logging.getLogger("line_commands")

_pending_unlocks: dict[str, dict] = {}
_lockouts: dict[str, dict] = {}

UNLOCK_CODE_TTL_SECONDS = 300
LOCKOUT_THRESHOLD = 3
LOCKOUT_DURATION_SECONDS = 900

MEAL_LABELS = {"朝食", "昼食", "夕食", "間食", "おやつ"}
STAMP_LABELS = {"起床", "お薬", "朝食", "昼食", "お風呂", "夕食", "就寝"}


def _is_locked_out(sender_id: str) -> tuple[bool, int]:
    info = _lockouts.get(sender_id)
    if not info or not info.get("until"):
        return False, 0
    if time.time() >= info["until"]:
        del _lockouts[sender_id]
        return False, 0
    return True, int(info["until"] - time.time())


def match_command(text: str) -> str:
    t = (text or "").strip()
    lower = t.lower()

    if t.startswith("登録 ") or t.startswith("登録　") or t == "登録":
        return "register_family"
    if t.startswith("登録解除") or t == "解除登録":
        return "unregister_family"
    if t in ("登録一覧", "メンバー", "誰が登録"):
        return "list_registered"
    if any(kw in t for kw in ("ロック解除", "解除", "アンロック")):
        return "unlock_request"
    if re.fullmatch(r"\d{4}", t):
        return "unlock_confirm"
    if any(kw in t for kw in ("状況", "様子", "今日")):
        return "status"
    if "最後" in t or "さっき" in t:
        return "last_meal"
    if any(kw in lower for kw in ("ヘルプ", "help", "コマンド", "使い方")):
        return "help"
    if any(kw in lower for kw in ("メニュー", "menu")):
        return "menu"
    if t.startswith("意見") or t.startswith("質問") or t.startswith("要望") or t.startswith("バグ"):
        return "feedback"
    if any(kw.lower() in lower for kw in (
        "リンク", "url", "つながらない", "繋がらない",
        "見れない", "みれない", "アクセス", "開けない", "接続",
    )):
        return "link"
    return "unknown"


def _last_meal_info(grandma_id: int = 1) -> tuple[str, int] | None:
    sessions = sessions_today(grandma_id)
    meals = [s for s in sessions if s.get("label") in MEAL_LABELS]
    if not meals:
        return None
    last = meals[-1]
    t = last.get("started_at")
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    minutes_ago = int((datetime.now() - t).total_seconds() / 60)
    return f"{last.get('label')} — {t.strftime('%H:%M')}", minutes_ago


def _today_toilet_count(grandma_id: int = 1) -> int:
    """今日のトイレイベント数を取得（ドアセンサーまたは家族記録）。"""
    conn = get_conn()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM events
               WHERE started_at LIKE ?
               AND (source = 'toilet' AND event_type = 'open'
                    OR source IN ('family_report', 'tablet_report') AND event_type = 'トイレ')
               AND (person_id = ? OR person_id IS NULL)""",
            (f"{today}%", grandma_id),
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def _bath_info(grandma_id: int = 1) -> str:
    """今日のお風呂の時刻情報を返す。"""
    sessions = sessions_today(grandma_id)
    bath_sessions = [s for s in sessions if s.get("label") == "お風呂"]
    if not bath_sessions:
        return "まだ"
    b = bath_sessions[-1]
    t = b.get("started_at")
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return f"済（{t.strftime('%H:%M')}）"


def _medicine_status() -> str:
    """お薬の予定と実績を返す。"""
    conn = get_conn()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        schedule = conn.execute(
            "SELECT timing, hour FROM medicine_schedule WHERE enabled = 1 ORDER BY hour"
        ).fetchall()
        if not schedule:
            return "（予定なし）"
        taken = conn.execute(
            """SELECT COUNT(*) as cnt FROM events
               WHERE started_at LIKE ?
               AND source IN ('family_report', 'tablet_report')
               AND event_type = 'お薬'""",
            (f"{today}%",),
        ).fetchone()["cnt"]
        return f"{taken}/{len(schedule)}回（予定: {'/'.join(m['timing'] for m in schedule)}）"
    finally:
        conn.close()


def _recent_witness_reports(limit: int = 3) -> list[str]:
    """家族が証人として記録した最新の報告を返す。"""
    conn = get_conn()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT event_type, started_at, raw_meta FROM events
               WHERE source = 'family_report' AND started_at LIKE ?
               ORDER BY started_at DESC LIMIT ?""",
            (f"{today}%", limit),
        ).fetchall()
        out = []
        for r in rows:
            t = r["started_at"]
            if isinstance(t, str):
                t = datetime.fromisoformat(t)
            out.append(f"{t.strftime('%H:%M')} {r['event_type']}")
        return out
    finally:
        conn.close()


def handle_status() -> str:
    grandma_id = 1
    sessions = sessions_today(grandma_id)
    meal_count = sum(1 for s in sessions if s.get("label") in MEAL_LABELS)
    done_labels = {s.get("label") for s in sessions}
    stamp_done = len(STAMP_LABELS & done_labels)

    state = get_device_state("rice_cooker")
    lock_status = "🔒 ロック中" if (state and state["is_locked"]) else "🔓 通常"

    last_info = _last_meal_info(grandma_id)
    last_line = f"{last_info[0]}（{last_info[1]}分前）" if last_info else "（記録なし）"

    toilet_count = _today_toilet_count(grandma_id)
    bath = _bath_info(grandma_id)
    medicine = _medicine_status()
    witnesses = _recent_witness_reports(3)

    lines = [
        "📊 今日の祖母の状況",
        "",
        f"🍴 食事回数: {meal_count}回",
        f"🕐 最後の食事: {last_line}",
        f"💊 お薬: {medicine}",
        f"🛁 お風呂: {bath}",
        f"🚽 トイレ: {toilet_count}回",
        f"⭐ スタンプ達成: {stamp_done}/{len(STAMP_LABELS)}",
        f"🍚 炊飯器: {lock_status}",
    ]
    if witnesses:
        lines.append("")
        lines.append("📋 家族の記録（最新）:")
        lines.extend(f"  • {w}" for w in witnesses)

    return "\n".join(lines)


def handle_last_meal() -> str:
    info = _last_meal_info()
    if not info:
        return "今日はまだ食事の記録がありません。"
    return f"🍴 最後の食事\n\n{info[0]}\n（{info[1]}分前）"


def handle_help() -> str:
    return (
        "📖 使えるコマンド\n\n"
        "「メニュー」— 主要メニューをボタンで表示\n"
        "「登録 <名前>」— 家族として登録（例: 登録 母）\n"
        "「登録解除」— 自分の登録を解除\n"
        "「登録一覧」— 登録済み家族を確認\n"
        "「状況」— 今日の様子まとめ\n"
        "「最後の食事」— 直近の食事時刻\n"
        "「リンク」— 最新の公開URL\n"
        "「ロック解除」— 炊飯器ロックを解除（確認コード付き）\n"
        "「意見 ＜内容＞」— 開発者への質問・要望\n"
        "「ヘルプ」— このメッセージ"
    )


def handle_menu() -> dict:
    """主要メニューをLINEで表示する辞書を返す。

    handle_message() 側がこれを Quick Reply としてレンダリングする。
    URL アクションは LINE 側で開かれる。
    """
    base_url = _load_public_base_url()
    return {
        "_type": "menu",
        "text": "📋 メニュー\n下のボタンから選んでください",
        "items": [
            {"label": "🏠 家族管理画面", "uri": f"{base_url}/family"},
            {"label": "📚 使い方ガイド", "uri": f"{base_url}/guide/"},
            {"label": "📷 顔学習", "uri": f"{base_url}/family/face-learning"},
            {"label": "📷 食事写真", "uri": f"{base_url}/family"},
            {"label": "💬 意見・質問", "data": "feedback_start"},
            {"label": "🆘 困った時", "uri": f"{base_url}/guide/troubleshooting"},
        ],
    }


def _load_public_base_url() -> str:
    """data/tunnel_url.txt から公開 URL を取得 (6/3〜 Tailscale Funnel の固定 URL)。"""
    from pathlib import Path
    url_file = Path(__file__).resolve().parent.parent / "data" / "tunnel_url.txt"
    if url_file.exists():
        try:
            url = url_file.read_text().strip()
            if url:
                return url.rstrip("/")
        except Exception:
            pass
    import os
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


def handle_feedback(text: str, sender_id: str) -> str:
    """意見・質問を開発者（admin）へ転送 + DB 保存。"""
    body = text.split(maxsplit=1)
    content = body[1].strip() if len(body) > 1 else ""
    if not content:
        return (
            "📝 意見・質問の送り方\n\n"
            "「意見 ご飯写真がうまく送れません」のように、\n"
            "「意見」「質問」「要望」「バグ」のいずれかに続けて内容を書いてください。"
        )

    from .notifier import resolve_confirmer_name, send_line_message, _admin_user_id
    from .db import transaction
    name = resolve_confirmer_name(sender_id)

    # DB 保存（feedback テーブルがあれば、なくてもログには残る）
    try:
        with transaction() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       sender_id TEXT, sender_name TEXT,
                       content TEXT NOT NULL,
                       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            c.execute(
                "INSERT INTO feedback(sender_id, sender_name, content) VALUES(?, ?, ?)",
                (sender_id, name, content),
            )
    except Exception as e:
        log.warning("feedback 保存失敗: %s", e)

    # 開発者(admin) へ即時転送
    admin = _admin_user_id()
    if admin:
        msg = f"💬 {name}さんから意見・質問\n\n{content}"
        try:
            send_line_message(msg, user_id=admin)
        except Exception as e:
            log.warning("feedback 転送失敗: %s", e)

    return f"✅ {name}さん、ご意見ありがとうございます。開発者に伝えました。"


def handle_unlock_request(sender_id: str) -> str:
    locked, remaining = _is_locked_out(sender_id)
    if locked:
        mins = (remaining + 59) // 60
        return f"⚠️ 連続失敗によりロック中です。あと約{mins}分お待ちください。"

    state = get_device_state("rice_cooker")
    if not state or not state["is_locked"]:
        return "🔓 炊飯器は既にロックされていません。"

    code = f"{random.randint(0, 9999):04d}"
    _pending_unlocks[sender_id] = {
        "code": code,
        "expires_at": time.time() + UNLOCK_CODE_TTL_SECONDS,
        "device": "rice_cooker",
    }
    log.info("ロック解除コード発行: sender=%s...", sender_id[:8])
    return (
        f"🔑 確認コード: {code}\n\n"
        "解除する場合は5分以内にこの4桁を返信してください。\n"
        "何もしなければ5分で自動的に無効化されます。"
    )


async def handle_unlock_confirm(text: str, sender_id: str) -> str:
    pending = _pending_unlocks.get(sender_id)
    if not pending:
        return "⚠️ 確認コードは発行されていません。「ロック解除」から始めてください。"

    if time.time() > pending["expires_at"]:
        del _pending_unlocks[sender_id]
        return "⌛ 確認コードの有効期限が切れました。もう一度「ロック解除」と送ってください。"

    if text.strip() != pending["code"]:
        info = _lockouts.setdefault(sender_id, {"count": 0, "until": 0})
        info["count"] += 1
        if info["count"] >= LOCKOUT_THRESHOLD:
            info["until"] = time.time() + LOCKOUT_DURATION_SECONDS
            _pending_unlocks.pop(sender_id, None)
            mins = LOCKOUT_DURATION_SECONDS // 60
            return f"❌ 連続{LOCKOUT_THRESHOLD}回失敗しました。{mins}分間コマンドをロックします。"
        remaining = LOCKOUT_THRESHOLD - info["count"]
        return f"❌ コードが違います。あと{remaining}回失敗するとロックアウトされます。"

    device = pending["device"]
    _pending_unlocks.pop(sender_id, None)
    _lockouts.pop(sender_id, None)

    node_id = 1
    try:
        success = await unlock_device(device, node_id, reason=f"LINE経由解除 (sender: {sender_id[:8]}...)")
    except Exception as e:
        log.error("ロック解除エラー: %s", e)
        return "⚠️ 解除処理でエラーが発生しました。"
    if success:
        # 全家族にbroadcast（誰がLINEから解除したか名前付き）
        try:
            from .notifier import notify_device_unlocked, resolve_confirmer_name
            name = resolve_confirmer_name(sender_id)
            await asyncio.to_thread(
                notify_device_unlocked, device, True, f"{name}さんがLINEから解除"
            )
        except Exception as e:
            log.warning("unlock broadcast失敗: %s", e)
        return "✅ 炊飯器のロックを解除しました。"
    return "⚠️ 解除処理に失敗しました（Matter通信エラー）。"


def handle_register_family(text: str, sender_id: str) -> str:
    """「登録 <名前>」で家族メンバーを登録する。自由な名前OK。

    既存persons（祖母・母・祖父など）と一致すればそれにリンク、
    新しい名前ならpersonsに新規行を追加してリンクする。
    """
    parts = text.replace("　", " ").split()
    if len(parts) < 2:
        return (
            "📝 家族登録\n\n"
            "「登録 <名前>」の形式で送ってください。\n"
            "例: 登録 母 / 登録 叔父 / 登録 たろう\n\n"
            "好きな呼び名でOKです。"
        )
    name = parts[1].strip()
    if not name:
        return "❌ 名前が空です。「登録 <名前>」と送ってください。"
    if len(name) > 20:
        return "❌ 名前は20文字以内にしてください。"
    if name == "未確定":
        return "❌ 「未確定」は予約名なので使えません。"

    # 既存personsを検索、なければ新規作成
    person_id: int
    is_new_person = False
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM persons WHERE name = ? AND id > 0", (name,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        person_id = row["id"]
    else:
        with transaction() as c:
            cur = c.execute(
                "INSERT INTO persons(name, role) VALUES(?, 'family')", (name,)
            )
            person_id = cur.lastrowid
        is_new_person = True

    # LINE登録（既存LINE-userの上書き許容）
    with transaction() as c:
        existing = c.execute(
            "SELECT person_id FROM family_line_users WHERE line_user_id = ?",
            (sender_id,),
        ).fetchone()
        c.execute(
            """INSERT INTO family_line_users(line_user_id, person_id, display_name, registered_at, last_seen_at)
               VALUES(?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(line_user_id) DO UPDATE SET
                   person_id = excluded.person_id,
                   display_name = excluded.display_name,
                   last_seen_at = CURRENT_TIMESTAMP""",
            (sender_id, person_id, name),
        )

    msg = f"✅ 「{name}」として登録しました。\n以降このLINEに通知が届きます。"
    if is_new_person:
        msg += "\n（新しい人物として作成しました）"
    if existing:
        msg += "\n（前の登録を上書きしました）"
    msg += "\n\n----------\n" + handle_help()
    return msg


def handle_unregister_family(sender_id: str) -> str:
    """登録解除"""
    conn = get_conn()
    try:
        existing = conn.execute(
            """SELECT f.line_user_id, p.name FROM family_line_users f
               LEFT JOIN persons p ON p.id = f.person_id
               WHERE f.line_user_id = ?""",
            (sender_id,),
        ).fetchone()
    finally:
        conn.close()
    if not existing:
        return "ℹ️ あなたはまだ登録されていません。"
    with transaction() as c:
        c.execute("DELETE FROM family_line_users WHERE line_user_id = ?", (sender_id,))
    return f"✅ 「{existing['name']}」としての登録を解除しました。\n通知は届かなくなります。"


def handle_list_registered() -> str:
    """登録メンバー一覧"""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT f.line_user_id, p.name, f.registered_at FROM family_line_users f
               LEFT JOIN persons p ON p.id = f.person_id
               ORDER BY f.registered_at"""
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "📋 登録済み家族はいません。\n「登録 <名前>」で登録できます。"
    lines = ["📋 登録済み家族:"]
    for r in rows:
        masked = r["line_user_id"][:6] + "..." + r["line_user_id"][-3:]
        lines.append(f"  • {r['name']} ({masked})")
    return "\n".join(lines)


CATEGORY_LABELS = {
    "meal_alert": "食事行動アラート",
    "device_locked": "炊飯器自動ロック",
    "bath_emergency": "浴室緊急通知",
    "anomaly_inactivity": "安否確認アラート",
    "anomaly_night_rice": "深夜炊飯器アラート",
    "anomaly_fridge_open": "冷蔵庫開放アラート",
    "medicine_reminder": "お薬リマインダー",
    "bath_reminder": "お風呂リマインダー",
    "tablet_unverified": "タブレットボタン未確認",
    "attribute_session": "未確定セッション人物確認",
}


async def handle_rice_action_postback(data: str, sender_id: str) -> str | None:
    """炊飯器の曖昧な power_on を家族が分類するボタン押下を処理。

    data形式: "rice_action:<event_id>:<action>"
      action: cook / keep_warm / lid_only / unknown
    家族の判定を rice_classifications テーブルに学習データとして保存する。
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "rice_action":
        return None
    try:
        event_id = int(parts[1])
    except ValueError:
        return None
    action = parts[2]

    from .notifier import resolve_confirmer_name, mark_notification_completed
    from datetime import datetime as _dt
    confirmer = resolve_confirmer_name(sender_id)

    action_label = {
        "cook": "炊飯",
        "keep_warm": "保温",
        "lid_only": "蓋を開けただけ",
        "lid_meal": "開けてご飯食べた",
        "unknown": "不明",
    }.get(action, action)

    # イベント情報取得（電力・時刻、学習データ保存用）
    power_w: float | None = None
    hour_of_day: int | None = None
    conn = get_conn()
    try:
        ev = conn.execute(
            "SELECT value, started_at FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if ev:
            power_w = float(ev["value"]) if ev["value"] is not None else None
            t = ev["started_at"]
            if isinstance(t, str):
                try:
                    t = _dt.fromisoformat(t)
                except ValueError:
                    t = None
            if isinstance(t, _dt):
                hour_of_day = t.hour
    finally:
        conn.close()

    # 学習データに記録（unknownでも将来の判別性は無いが履歴として残す）
    if power_w is not None and hour_of_day is not None:
        try:
            with transaction() as c:
                c.execute(
                    """INSERT INTO rice_classifications
                           (event_id, power_w, hour_of_day, lid_recently_opened,
                            classification, classified_by, auto_decided)
                       VALUES(?, ?, ?, 0, ?, ?, 0)""",
                    (event_id, power_w, hour_of_day, action, sender_id),
                )
        except Exception as e:
            log.warning("rice_classifications 保存失敗: %s", e)

    # 保温/蓋開のみ → イベント削除＋関連セッションも空ならクリーンアップ
    if action in ("keep_warm", "lid_only"):
        with transaction() as c:
            session_ids = [
                r["session_id"]
                for r in c.execute(
                    "SELECT session_id FROM session_events WHERE event_id = ?",
                    (event_id,),
                ).fetchall()
            ]
            c.execute("DELETE FROM session_events WHERE event_id = ?", (event_id,))
            c.execute("DELETE FROM events WHERE id = ?", (event_id,))
            for sid in session_ids:
                remaining = c.execute(
                    "SELECT COUNT(*) FROM session_events WHERE session_id = ?",
                    (sid,),
                ).fetchone()[0]
                if remaining == 0:
                    c.execute("DELETE FROM meal_sessions WHERE id = ?", (sid,))
        summary = f"炊飯器の動作を「{action_label}」として記録しました（食事ではないので食事回数には数えません）"
    elif action in ("cook", "lid_meal"):
        # 食事として確定 — meal_sessions の集約に拾わせるため event はそのまま残す
        summary = f"炊飯器の動作を「{action_label}」として記録しました（祖母さんの食事1回として記録）"
    else:  # unknown
        summary = "炊飯器の動作を「判定保留」にしました（次回似た動きが来たら学習します）"

    # 学習が進んだら自動判定が効くようになるヒントを追加
    cls_count = 0
    try:
        conn = get_conn()
        cls_count = conn.execute(
            "SELECT COUNT(*) FROM rice_classifications WHERE auto_decided = 0 AND classification != 'unknown'"
        ).fetchone()[0]
        conn.close()
    except Exception:
        pass

    if cls_count > 0 and cls_count % 5 == 0:
        summary += f"\n📚 学習サンプル {cls_count}件 蓄積済み（類似ケースは自動判定されます）"

    await asyncio.to_thread(
        mark_notification_completed,
        "rice_action", f"event_{event_id}", sender_id, summary,
    )

    # keep_warm/lid_only 時は同日の他の rice_action 通知も自動でサイレント完了
    # （炊飯器が保温で動きっぱなしの状況で、複数の通知が連発するのを抑制）
    extra_closed = 0
    if action in ("keep_warm", "lid_only"):
        from .notifier import mark_related_completed_silent
        from datetime import datetime as _dt2
        today_prefix = _dt2.now().strftime("%Y-%m-%d")
        # context_key は "event_<id>" 形式なので prefix では拾えない。
        # よって event_id ごとに直接拾わず、rice_action 全体を完了 + ただし当日作成のみ。
        # mark_related_completed_silent は context_key LIKE で動くため、
        # rice_action 全 pending を当日分として消す方針。
        try:
            with transaction() as c:
                cur = c.execute(
                    """UPDATE pending_notifications
                          SET completed_at = CURRENT_TIMESTAMP,
                              completed_by = ?,
                              completed_action = ?
                        WHERE notification_type = 'rice_action'
                          AND completed_at IS NULL
                          AND created_at >= datetime('now', '-24 hours')""",
                    (sender_id[:64], f"同日の保温分類により自動完了（{action_label}）"),
                )
                extra_closed = cur.rowcount or 0
        except Exception as e:
            log.warning("rice_action 関連通知の自動完了失敗: %s", e)
        if extra_closed:
            log.info("[rice_action] 関連通知を %d件 自動完了", extra_closed)

    msg = f"✅ {confirmer}さん、{summary}"
    if extra_closed:
        msg += f"\n💡 他の同種 {extra_closed}件の通知も同時に閉じました"
    return msg


async def handle_session_confirm_postback(data: str, sender_id: str) -> str | None:
    """新規食事セッションへの家族確認: 誰の食事か / 食事じゃないか。

    data形式: "sess_confirm:<session_id>:<choice>"
      choice: "1" (祖母) / "2" (母) / "3" (祖父) / "other" / "reject"
    confirmed=1 で確定、confirmed=-1 で却下（UIから消える）。
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "sess_confirm":
        return None
    try:
        sid = int(parts[1])
    except ValueError:
        return None
    choice = parts[2]

    from .notifier import resolve_confirmer_name, mark_notification_completed
    name = resolve_confirmer_name(sender_id)

    if choice == "reject":
        # 誤検知 → confirmed=-1 にしてUIから隠す（生イベントは残す）
        try:
            with transaction() as c:
                c.execute(
                    """UPDATE meal_sessions
                          SET confirmed = -1, confirmed_by = ?, confirmed_at = CURRENT_TIMESTAMP
                        WHERE id = ?""",
                    (sender_id[:64], sid),
                )
        except Exception as e:
            log.warning("セッション却下失敗: %s", e)
            return "⚠️ 処理に失敗しました"
        await asyncio.to_thread(
            mark_notification_completed,
            "session_confirm", f"session_{sid}", sender_id,
            f"{name}さんが「食事じゃない」を選択 → 食事カウントから除外",
        )
        return f"✅ {name}さん、誤検知として記録しました"

    # person_id を確定
    if choice in ("1", "2", "3"):
        person_id = int(choice)
    elif choice == "other":
        person_id = 0  # 未確定（後で家族管理画面で詳細指定可）
    else:
        return None

    # セッション情報取得
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT label, started_at FROM meal_sessions WHERE id = ?", (sid,)
        ).fetchone()
        if person_id:
            person = conn.execute(
                "SELECT name FROM persons WHERE id = ?", (person_id,)
            ).fetchone()
            person_name = person["name"] if person else "?"
        else:
            person_name = "他の家族"
    finally:
        conn.close()
    if not row:
        return "⚠️ 該当セッションが見つかりません"
    label = row["label"] or "食事"

    try:
        with transaction() as c:
            c.execute(
                """UPDATE meal_sessions
                      SET person_id = ?, confirmed = 1,
                          confirmed_by = ?, confirmed_at = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                (person_id, sender_id[:64], sid),
            )
    except Exception as e:
        log.warning("セッション確定失敗: %s", e)
        return "⚠️ 処理に失敗しました"

    await asyncio.to_thread(
        mark_notification_completed,
        "session_confirm", f"session_{sid}", sender_id,
        f"{label}を「{person_name}さん」の食事として記録",
    )
    return f"✅ {name}さん、{person_name}さんの{label}として記録しました"


async def handle_lock_confirm_postback(data: str, sender_id: str) -> str | None:
    """炊飯器ロック確認リクエストへの家族の回答を処理。

    data形式: "lock_confirm:<ctx>:yes" or "lock_confirm:<ctx>:no"
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "lock_confirm":
        return None
    ctx = parts[1]
    choice = parts[2]
    if choice not in ("yes", "no"):
        return None

    from .notifier import resolve_confirmer_name, mark_notification_completed
    name = resolve_confirmer_name(sender_id)

    if choice == "no":
        await asyncio.to_thread(
            mark_notification_completed,
            "lock_confirm", ctx, sender_id,
            f"{name}さんが「ロックしない」を選択 → スキップ",
        )
        return f"✅ {name}さん、ロックをスキップしました"

    # yes: 実際にロック実行
    from .lock_manager import lock_device
    import os
    rice_node_id = int(os.environ.get("RICE_COOKER_NODE_ID", "1"))
    try:
        locked = await lock_device(
            "rice_cooker", rice_node_id,
            reason=f"家族（{name}）の確認後にロック",
        )
    except Exception as e:
        log.warning("ロック実行失敗: %s", e)
        locked = False

    if not locked:
        await asyncio.to_thread(
            mark_notification_completed,
            "lock_confirm", ctx, sender_id,
            f"{name}さんが承認したがロック実行に失敗",
        )
        return f"⚠️ {name}さん、承認は受けましたがロック実行に失敗しました（matter通信エラー）"

    # ロック確認はもう取れているので、追加の actionable 通知は出さず
    # シンプルな完了 broadcast のみで重複確認を避ける
    await asyncio.to_thread(
        mark_notification_completed,
        "lock_confirm", ctx, sender_id,
        f"{name}さんが承認 → 🔒 炊飯器ロック実行",
    )
    return f"✅ {name}さん、炊飯器をロックしました"


async def handle_bath_classification_postback(data: str, sender_id: str) -> str | None:
    """お風呂利用候補に対する家族の回答を学習データとして保存する。

    data形式: "bath_cls:<record_id>:<choice>"
      choice: grandma / grandpa / mother / other / yu_filling / cleaning / no_one
    bath_classifications.confirmed_person_id / confirmed_kind を更新。
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "bath_cls":
        return None
    try:
        record_id = int(parts[1])
    except ValueError:
        return None
    choice = parts[2]

    # 選択肢 → (person_id, kind, label) のマッピング
    # person_id=0: 誰もいない（湯はり/清掃）, person_id>0: その人物
    choice_map = {
        "grandma": (1, "bathing", "祖母さんの入浴"),
        "grandpa": (3, "bathing", "祖父さんの入浴"),
        "mother": (2, "bathing", "母さんの入浴"),
        "other": (None, "bathing", "他の家族の入浴"),
        "yu_filling": (0, "yu_filling", "湯はり（人なし）"),
        "cleaning": (0, "cleaning", "清掃"),
        "no_one": (0, "unknown", "誰もいない"),
    }
    if choice not in choice_map:
        return None
    person_id, kind, label = choice_map[choice]

    from .notifier import resolve_confirmer_name, mark_notification_completed
    confirmer = resolve_confirmer_name(sender_id)

    try:
        with transaction() as c:
            row = c.execute(
                "SELECT id, confirmed_person_id FROM bath_classifications WHERE id = ?",
                (record_id,),
            ).fetchone()
            if not row:
                return "⚠️ 該当の入浴記録が見つかりません"
            if row["confirmed_person_id"] is not None:
                return "ℹ️ この入浴は既に他の家族が回答済みです"
            c.execute(
                """UPDATE bath_classifications
                      SET confirmed_person_id = ?,
                          confirmed_kind = ?,
                          confirmation_method = 'line_reply',
                          confirmed_by = ?,
                          confirmed_at = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                (person_id if person_id is not None else -1,
                 kind, sender_id[:64], record_id),
            )
    except Exception as e:
        log.warning("bath_classifications 更新失敗: %s", e)
        return "⚠️ 記録に失敗しました"

    # 関連 pending_notification も完了マーク
    await asyncio.to_thread(
        mark_notification_completed,
        "bath_classification", f"bath_{record_id}", sender_id,
        f"お風呂使用「{label}」として記録",
    )
    return f"✅ {confirmer}さん、{label}として記録しました"


async def handle_confirm_dismiss_postback(data: str, sender_id: str) -> str | None:
    """「対応不要（誤検知）」ボタン押下: 通知を完了マーク + 学習用に記録。

    data形式: "confirm_dismiss:<category>:<context_key>"
    完了は記録するが、対応済みではなく「誤検知」としてマーク。
    """
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "confirm_dismiss":
        return None
    category, context_key = parts[1], parts[2]
    label = CATEGORY_LABELS.get(category, category)
    from .notifier import mark_notification_completed, resolve_confirmer_name
    name = resolve_confirmer_name(sender_id)
    success = await asyncio.to_thread(
        mark_notification_completed,
        category, context_key, sender_id,
        f"「{label}」を誤検知として閉じました",
    )
    if success:
        return f"✅ {name}さん、誤検知として記録しました（再通知停止）"
    return "ℹ️ この通知は既に他の家族が対応済みです"


async def handle_confirm_postback(data: str, sender_id: str) -> str | None:
    """「✓ 確認した」ボタン押下を処理する。

    data形式: "confirm:<category>:<context_key>"
    既に他の家族が対応済みなら何もしない（先勝ちで競合ガード）。
    対応成立した場合は全家族に「☑️ <名前>さんが対応しました」をbroadcast。
    """
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "confirm":
        return None
    category, context_key = parts[1], parts[2]

    label = CATEGORY_LABELS.get(category, category)
    from .notifier import (
        mark_notification_completed, mark_related_completed_silent,
        resolve_confirmer_name,
    )
    name = resolve_confirmer_name(sender_id)
    success = await asyncio.to_thread(
        mark_notification_completed,
        category, context_key, sender_id,
        f"「{label}」を確認",
    )
    if success:
        # 関連通知の自動完了: meal_alertとdevice_lockedは同じ食事イベントから派生するので
        # 片方の確認で同日の関連通知も同時にクローズする（再通知ループ防止）
        # context_keyの先頭は YYYY-MM-DD（例: "2026-05-04_3" や "2026-05-04_1629_rice_cooker"）
        date_prefix = context_key.split("_")[0] if "_" in context_key else context_key
        if len(date_prefix) == 10 and date_prefix[4] == "-":  # YYYY-MM-DD 検査
            related_pairs = []
            if category == "device_locked":
                related_pairs.append(("meal_alert", date_prefix))
            elif category == "meal_alert":
                related_pairs.append(("device_locked", date_prefix))
            for rel_type, rel_prefix in related_pairs:
                closed = await asyncio.to_thread(
                    mark_related_completed_silent,
                    rel_type, rel_prefix, sender_id,
                    f"関連通知（{label}）の確認時に同時クローズ",
                )
                if closed:
                    log.info("関連通知 %s を %d件 自動完了", rel_type, closed)
        return f"✅ {name}さん、確認を記録しました。家族全員に共有しました。"
    return "ℹ️ この通知は既に他の家族が対応済みです。"


async def handle_merge_postback(data: str, sender_id: str) -> str | None:
    """「前と同じ食事」ボタン押下を処理してセッションを統合する。

    data形式: "merge:<new_session_id>:<prev_session_id>"
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "merge":
        return None
    try:
        new_sid = int(parts[1])
        prev_sid = int(parts[2])
    except ValueError:
        return None

    success = merge_sessions_manual(new_sid, prev_sid)
    if not success:
        return "⚠️ 統合に失敗しました（対象の食事記録が見つかりません）"

    # 統合後のセッション情報を返信
    conn = get_conn()
    try:
        merged = conn.execute(
            """SELECT m.id, m.label, m.started_at, m.ended_at, p.name as person_name
                 FROM meal_sessions m LEFT JOIN persons p ON p.id = m.person_id
                WHERE m.id = ?""",
            (prev_sid,),
        ).fetchone()
    finally:
        conn.close()

    person = (merged["person_name"] if merged else None) or "未確定"
    label = merged["label"] if merged else "セッション"
    action_summary = f"今回の食事を、前回の{label}（{person}さん）と同じ食事としてまとめました"

    # 通知完了マーク + 全家族へのブロードキャスト
    try:
        from .notifier import mark_notification_completed
        await asyncio.to_thread(
            mark_notification_completed,
            "attribute_session", f"session_{new_sid}",
            sender_id, action_summary,
        )
    except Exception as e:
        log.warning("完了ブロードキャスト失敗: %s", e)

    return f"✅ 「前と同じ食事」として統合しました。\n{action_summary}"


async def handle_attribute_postback(data: str, sender_id: str) -> str | None:
    """Quick Reply の postback データを処理してセッションの person_id を確定する。

    data形式: "attribute:<session_id>:<person_id>"
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "attribute":
        return None
    try:
        session_id = int(parts[1])
        new_person_id = int(parts[2])
    except ValueError:
        return None

    # personsの存在チェック
    conn = get_conn()
    try:
        person_row = conn.execute(
            "SELECT id, name FROM persons WHERE id = ?", (new_person_id,)
        ).fetchone()
        session_row = conn.execute(
            "SELECT id, person_id, started_at, label FROM meal_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    person = dict(person_row) if person_row else None
    session = dict(session_row) if session_row else None

    if not person:
        return f"⚠️ person_id={new_person_id} は登録されていません。"
    if not session:
        return "⚠️ 対象の記録が見つかりません"

    old_person_id = session["person_id"]

    # 既に確定済みなら上書き確認
    if old_person_id and old_person_id != 0 and old_person_id != new_person_id:
        old_name = "?"
        try:
            conn = get_conn()
            r = conn.execute("SELECT name FROM persons WHERE id = ?", (old_person_id,)).fetchone()
            if r:
                old_name = r["name"]
            conn.close()
        except Exception:
            pass
        log.info("セッション #%d: %s → %s に変更", session_id, old_name, person["name"])

    # セッションと紐づくイベントの person_id を更新
    with transaction() as conn:
        conn.execute(
            "UPDATE meal_sessions SET person_id = ? WHERE id = ?",
            (new_person_id, session_id),
        )
        conn.execute(
            """UPDATE events SET person_id = ?
               WHERE id IN (SELECT event_id FROM session_events WHERE session_id = ?)""",
            (new_person_id, session_id),
        )

    # ロック判定（祖母確定の場合のみ再評価）
    lock_msg = ""
    if new_person_id == 1:  # 祖母
        try:
            from .lock_manager import lock_device, should_warn_recent_meal, RECENT_MEAL_MINUTES
            from .notifier import notify_meal_alert
            grandma_sessions = sessions_today(1)
            meal_sessions = [s for s in grandma_sessions if s.get("label") in MEAL_LABELS]
            if len(meal_sessions) >= 2:
                # 直近90分以内に2回以上の食事 → ロック
                state = get_device_state("rice_cooker")
                if not (state and state["is_locked"]):
                    warn = should_warn_recent_meal(1)
                    if warn:
                        success = await lock_device("rice_cooker", 1, reason="人物割当後の自動ロック")
                        if success:
                            lock_msg = "\n🔒 直近食事が確認されたため炊飯器をロックしました"
        except Exception as e:
            log.warning("ロック再評価失敗: %s", e)

    action_summary = (
        f"{session.get('label') or '食事'}を「{person['name']}さん」のものとして記録しました"
        + lock_msg
    )
    # 全家族に完了をブロードキャスト（自動でpending_notification完了マークも）
    try:
        from .notifier import mark_notification_completed
        await asyncio.to_thread(
            mark_notification_completed,
            "attribute_session", f"session_{session_id}",
            sender_id, action_summary,
        )
    except Exception as e:
        log.warning("完了ブロードキャスト失敗: %s", e)

    return f"✅ {action_summary}"


async def dispatch(text: str, sender_id: str) -> str | None:
    """メッセージを解釈して返信テキストを返す。Noneなら返信しない。

    "link" はここでは処理せず、webhook側でURLを組み立てる。
    """
    cmd = match_command(text)
    if cmd == "register_family":
        return handle_register_family(text, sender_id)
    if cmd == "unregister_family":
        return handle_unregister_family(sender_id)
    if cmd == "list_registered":
        return handle_list_registered()
    if cmd == "unlock_request":
        return handle_unlock_request(sender_id)
    if cmd == "unlock_confirm":
        return await handle_unlock_confirm(text, sender_id)
    if cmd == "status":
        return handle_status()
    if cmd == "last_meal":
        return handle_last_meal()
    if cmd == "help":
        return handle_help()
    if cmd == "menu":
        return handle_menu()  # dict 返却 → webhook側で Quick Reply としてレンダリング
    if cmd == "feedback":
        return handle_feedback(text, sender_id)
    return None
