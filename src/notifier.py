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
