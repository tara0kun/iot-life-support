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
        "「登録 <名前>」— 家族として登録（例: 登録 母）\n"
        "「登録解除」— 自分の登録を解除\n"
        "「登録一覧」— 登録済み家族を確認\n"
        "「状況」— 今日の様子まとめ\n"
        "「最後の食事」— 直近の食事時刻\n"
        "「リンク」— 最新の公開URL\n"
        "「ロック解除」— 炊飯器ロックを解除（確認コード付き）\n"
        "「ヘルプ」— このメッセージ"
    )


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
        return "✅ 炊飯器のロックを解除しました。"
    return "⚠️ 解除処理に失敗しました（Matter通信エラー）。"


def handle_register_family(text: str, sender_id: str) -> str:
    """「登録 <名前>」で家族メンバーを登録する。

    例: 登録 母 / 登録 祖父 / 登録 孫
    """
    parts = text.replace("　", " ").split()
    if len(parts) < 2:
        return (
            "📝 家族登録\n\n"
            "「登録 <名前>」の形式で送ってください。\n"
            "例: 登録 母\n\n"
            "登録できる名前は「登録一覧」で確認できます。"
        )
    name = parts[1].strip()
    conn = get_conn()
    try:
        person = conn.execute(
            "SELECT id, name FROM persons WHERE name = ? AND id > 0",
            (name,),
        ).fetchone()
        if not person:
            valid_names = [
                r["name"]
                for r in conn.execute("SELECT name FROM persons WHERE id > 0 ORDER BY id").fetchall()
            ]
            return (
                f"❌ 「{name}」は登録できる名前ではありません。\n"
                f"使える名前: {', '.join(valid_names)}"
            )
    finally:
        conn.close()

    # 既存登録上書きを許容（家族の機種変更等に対応）
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
            (sender_id, person["id"], name),
        )

    msg = f"✅ 「{name}」として登録しました。\n以降このLINEに通知が届きます。"
    if existing:
        msg += "\n（前の登録を上書きしました）"
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
        return f"⚠️ 統合に失敗しました（セッション #{new_sid} または #{prev_sid} が見つかりません）。"

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
    action_summary = f"#{new_sid} を #{prev_sid} ({label} / {person}) に統合"

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
        person = conn.execute(
            "SELECT id, name FROM persons WHERE id = ?", (new_person_id,)
        ).fetchone()
        session = conn.execute(
            "SELECT id, person_id, started_at, label FROM meal_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    if not person:
        return f"⚠️ person_id={new_person_id} は登録されていません。"
    if not session:
        return f"⚠️ セッション #{session_id} は見つかりません。"

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
        f"{session.get('label') or 'セッション'} #{session_id} を「{person['name']}」として記録"
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
    return None
