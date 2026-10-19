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


def _status_of(reason: str) -> int | None:
    """`_push` が返す "HTTP 400: ..." から状態コードを取り出す。"""
    import re

    m = re.match(r"HTTP (\d{3})", reason or "")
    return int(m.group(1)) if m else None


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

        # **1行の不備で main() を落としてはいけない。** 以前は entry["to"] を
        # 素のキー参照で取っていたため、to が無い行（notifier 側の仕様変更や、
        # 電源断で途中まで書かれた行）で KeyError になり main() が中断していた。
        # ファイルの書き戻しに到達しないので、
        #   - その行より後ろの通知が永久に送られない（浴室の緊急通知を含む）
        #   - 手前の行は毎分 push され続ける（家族に同じ通知が毎分届く）
        #   - retry_count も TTL も進まないので自然に止まらない
        # という状態になっていた（2026-10-15 の監査で発覚）。
        to = entry.get("to")
        messages = entry.get("messages")
        if not to or not messages:
            log_broken = entry.get("queued_at", "?")
            print(f"outbox: 不正な行を破棄 (queued_at={log_broken})")
            dropped += 1
            continue

        # 4xx は何回送っても通らない。429 だけは時間をおけば通るので再送する。
        try:
            ok, reason = _push(token, to, messages)
        except Exception as e:
            ok, reason = False, f"送信例外: {e}"

        if ok:
            sent += 1
            continue

        status = _status_of(reason)
        if status is not None and 400 <= status < 500 and status != 429:
            print(f"outbox: 恒久エラーのため破棄 (HTTP {status})")
            dropped += 1
            continue

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
