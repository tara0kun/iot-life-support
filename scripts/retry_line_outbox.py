"""LINE outbox 再送スクリプト。

ネットワーク断時に `src.notifier._enqueue_outbox` が data/line_outbox.jsonl に
積んだメッセージを cron で再送試行する。

cron: `* * * * *` (1分毎)
TTL: 24時間以上古いエントリは諦めて破棄（chronic outage 時の無限増加防止）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUTBOX = ROOT / "data" / "line_outbox.jsonl"
MAX_AGE_HOURS = 24
MAX_RETRIES = 50  # 1分毎なので50分試行で諦め


def _push(token: str, to: str, messages: list[dict]) -> tuple[bool, str]:
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={"to": to, "messages": messages},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, ""
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        return False, f"net: {e}"
    except Exception as e:
        return False, f"err: {e}"


def main() -> int:
    if not OUTBOX.exists():
        return 0
    load_dotenv(ROOT / ".env")
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        print("LINE_CHANNEL_ACCESS_TOKEN 未設定", file=sys.stderr)
        return 1

    try:
        lines = OUTBOX.read_text().splitlines()
    except Exception:
        return 0

    remaining: list[str] = []
    sent, dropped, requeued = 0, 0, 0
    now = datetime.now()

    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        # TTL チェック
        try:
            queued = datetime.fromisoformat(entry.get("queued_at", ""))
            if now - queued > timedelta(hours=MAX_AGE_HOURS):
                dropped += 1
                continue
        except Exception:
            pass
        if entry.get("retry_count", 0) >= MAX_RETRIES:
            dropped += 1
            continue

        ok, reason = _push(token, entry["to"], entry["messages"])
        if ok:
            sent += 1
        else:
            entry["retry_count"] = entry.get("retry_count", 0) + 1
            entry["last_error"] = reason
            remaining.append(json.dumps(entry, ensure_ascii=False))
            requeued += 1

    if remaining:
        OUTBOX.write_text("\n".join(remaining) + "\n")
    else:
        OUTBOX.unlink(missing_ok=True)

    if sent or dropped:
        print(f"outbox: sent={sent} requeued={requeued} dropped={dropped} (total={len(lines)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
