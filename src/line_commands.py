"""LINE webhookで受信したメッセージを解釈して応答文字列を返すディスパッチャ。

優先順位: ロック解除 > 確認コード(数字4桁) > 状況 > 最後の食事 > ヘルプ > リンク
確認コードとロックアウト情報はプロセス内メモリに保持（再起動で消える）。
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime

from .db import get_conn
from .lock_manager import get_device_state, unlock_device
from .sessions import sessions_today

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

    if any(kw in t for kw in ("ロック解除", "解除", "アンロック")):
        return "unlock_request"
    if re.fullmatch(r"\d{4}", t):
        return "unlock_confirm"
    if t in ("済", "完了", "done", "done!") or t.startswith("済 ") or t.startswith("完了 "):
        return "task_done"
    if any(kw in t for kw in ("タスク", "担当", "今日の仕事")):
        return "task_list"
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


def handle_task_list() -> str:
    """今日のタスク一覧と完了状況を返す。"""
    conn = get_conn()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT t.id, t.task_name, t.assignee_name, t.reminder_hour,
                      l.done_by, l.done_at
               FROM care_tasks t
               LEFT JOIN care_task_logs l ON l.task_id = t.id AND l.date = ?
               WHERE t.enabled = 1
               ORDER BY t.reminder_hour""",
            (today,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "📋 タスクは登録されていません。\n家族管理画面で登録してください。"
    lines = ["📋 今日のタスク", ""]
    for r in rows:
        mark = "✅" if r["done_by"] else "⬜"
        assignee = r["assignee_name"] or "未割当"
        hour = f"{r['reminder_hour']:02d}:00" if r["reminder_hour"] is not None else "--:--"
        lines.append(f"{mark} {hour} {r['task_name']} [{assignee}]")
        if r["done_by"]:
            lines.append(f"    └ {r['done_by']} が完了")
    lines.append("")
    lines.append("対応したら「済 タスク名」と返信してください（例: 済 朝のお薬確認）")
    return "\n".join(lines)


def handle_task_done(text: str, sender_id: str) -> str:
    """「済 タスク名」でタスクを完了記録する。"""
    t = text.strip()
    # 「済」のみなら最も近い未完了タスクを補完候補に
    task_name = ""
    for prefix in ("済 ", "済　", "完了 ", "完了　"):
        if t.startswith(prefix):
            task_name = t[len(prefix):].strip()
            break

    conn = get_conn()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        if not task_name:
            # 今日の未完了タスクを列挙
            rows = conn.execute(
                """SELECT t.task_name FROM care_tasks t
                   LEFT JOIN care_task_logs l ON l.task_id = t.id AND l.date = ?
                   WHERE t.enabled = 1 AND l.id IS NULL
                   ORDER BY t.reminder_hour""",
                (today,),
            ).fetchall()
            if not rows:
                return "✅ 今日のタスクは全て完了しています。"
            names = "、".join(r["task_name"] for r in rows)
            return f"どのタスクを完了しましたか？\n「済 タスク名」の形式で送ってください。\n\n未完了: {names}"
        # タスク検索（部分一致）
        task = conn.execute(
            "SELECT id, task_name, assignee_name FROM care_tasks WHERE task_name LIKE ? AND enabled = 1",
            (f"%{task_name}%",),
        ).fetchone()
        if not task:
            return f"⚠️ タスク「{task_name}」が見つかりません。「タスク」で一覧を確認してください。"

        # 既に完了済みかチェック
        existing = conn.execute(
            "SELECT done_by, done_at FROM care_task_logs WHERE task_id = ? AND date = ?",
            (task["id"], today),
        ).fetchone()
        if existing:
            return f"ℹ️ 「{task['task_name']}」は既に {existing['done_by']} が対応済みです。"

        # 完了記録
        conn.execute(
            "INSERT INTO care_task_logs(task_id, date, done_by) VALUES(?, ?, ?)",
            (task["id"], today, sender_id[:8] + "..."),
        )
        conn.commit()
        return (
            f"✅ 「{task['task_name']}」の完了を記録しました。\n"
            f"家族全員に共有されます。"
        )
    finally:
        conn.close()


def handle_help() -> str:
    return (
        "📖 使えるコマンド\n\n"
        "「状況」— 今日の様子まとめ\n"
        "「最後の食事」— 直近の食事時刻\n"
        "「タスク」— 今日の家族タスク一覧\n"
        "「済 タスク名」— タスクを完了記録\n"
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


async def dispatch(text: str, sender_id: str) -> str | None:
    """メッセージを解釈して返信テキストを返す。Noneなら返信しない。

    "link" はここでは処理せず、webhook側でURLを組み立てる。
    """
    cmd = match_command(text)
    if cmd == "unlock_request":
        return handle_unlock_request(sender_id)
    if cmd == "unlock_confirm":
        return await handle_unlock_confirm(text, sender_id)
    if cmd == "task_list":
        return handle_task_list()
    if cmd == "task_done":
        return handle_task_done(text, sender_id)
    if cmd == "status":
        return handle_status()
    if cmd == "last_meal":
        return handle_last_meal()
    if cmd == "help":
        return handle_help()
    return None
