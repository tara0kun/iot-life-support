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

    return (
        "📊 今日の祖母の状況\n\n"
        f"🍴 食事回数: {meal_count}回\n"
        f"⭐ スタンプ達成: {stamp_done}/{len(STAMP_LABELS)}\n"
        f"🕐 最後の食事: {last_line}\n"
        f"🍚 炊飯器: {lock_status}"
    )


def handle_last_meal() -> str:
    info = _last_meal_info()
    if not info:
        return "今日はまだ食事の記録がありません。"
    return f"🍴 最後の食事\n\n{info[0]}\n（{info[1]}分前）"


def handle_help() -> str:
    return (
        "📖 使えるコマンド\n\n"
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


async def dispatch(text: str, sender_id: str) -> str | None:
    """メッセージを解釈して返信テキストを返す。Noneなら返信しない。

    "link" はここでは処理せず、webhook側でURLを組み立てる。
    """
    cmd = match_command(text)
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
