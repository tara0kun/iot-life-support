#!/usr/bin/env python3
"""セッションの確定状態まわりの回帰テスト（DB不要・実装からSQLを抜き出して検証）。

背景:
  お風呂セッションは monitor._request_session_confirmation が LINE 確認を
  意図的にスキップするため confirmed=1 になる経路が無い。にもかかわらず
  sessions_today() の既定が confirmed=1 のみだったため、毎日「未入浴」と
  誤判定され 18時の誤リマインドが出ていた。
  家族/タブレットの手動記録も confirmed を指定せず既定の 0 で入るため、
  「記録しました」と表示しても食事回数やお花に反映されなかった。

使い方:
    python scripts/test_session_confirm.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE meal_sessions(
    id INTEGER PRIMARY KEY,
    person_id INTEGER,
    started_at TEXT,
    ended_at TEXT,
    event_count INTEGER,
    label TEXT,
    confirmed INTEGER DEFAULT 0,
    confirmed_by TEXT,
    confirmed_at TIMESTAMP
)
"""

# (label, confirmed, 期待: 既定クエリに含まれるか, 説明)
CASES = [
    ("お風呂", 0, True, "確認フローを通らない → 数える"),
    ("お風呂", 1, True, "明示的に確定済 → 数える"),
    ("お風呂", -1, False, "家族が誤検知として却下 → 数えない"),
    ("夕食", 0, False, "未確定 → 従来どおり数えない"),
    ("夕食", 1, True, "確定済 → 数える"),
    ("夕食", -1, False, "却下 → 数えない"),
]


def _default_query() -> str:
    """sessions_today() の既定（確定済のみ）クエリを実装から抜き出す。"""
    src = (ROOT / "src" / "sessions.py").read_text(encoding="utf-8")
    found = [
        q
        for q in re.findall(r'"""(SELECT id, started_at.*?ORDER BY started_at)"""', src, re.S)
        if "confirmed = 1" in q
    ]
    if len(found) != 1:
        raise AssertionError(f"既定クエリの抽出に失敗しました ({len(found)} 件ヒット)")
    return found[0]


def _manual_inserts() -> list[str]:
    """web/app.py の手動記録 INSERT を抜き出す。"""
    app = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    found = re.findall(r'"""(INSERT INTO meal_sessions\(person_id.*?\))"""', app, re.S)
    return [q for q in found if "event_count" in q and "VALUES" in q]


def main() -> int:
    failures = 0
    conn = sqlite3.connect(":memory:")
    conn.execute(SCHEMA)

    query = _default_query()
    for i, (label, confirmed, _want, _why) in enumerate(CASES, start=1):
        conn.execute(
            "INSERT INTO meal_sessions VALUES(?,1,?,?,1,?,?,NULL,NULL)",
            (i, "2026-09-15 12:00:00", "2026-09-15 12:10:00", label, confirmed),
        )

    included = {r[0] for r in conn.execute(query, (1, "2026-09-15 00:00:00")).fetchall()}
    print("[sessions_today の既定クエリ]")
    for i, (label, confirmed, want, why) in enumerate(CASES, start=1):
        ok = (i in included) == want
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<4} confirmed={confirmed:>2} "
              f"期待={'含む' if want else '除外'}  {why}")

    inserts = _manual_inserts()
    print(f"\n[家族/タブレットの手動記録 INSERT: {len(inserts)} 箇所]")
    if not inserts:
        print("  FAIL  手動記録の INSERT が見つかりません")
        failures += 1
    for idx, stmt in enumerate(inserts, start=1):
        conn.execute(stmt, (1, "2026-09-15 13:00:00", "2026-09-15 13:00:00", "昼食"))
        row = conn.execute(
            "SELECT confirmed, confirmed_by FROM meal_sessions "
            "WHERE label='昼食' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        ok = row == (1, "family_ui")
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {idx}箇所目 → confirmed={row[0]} confirmed_by={row[1]}")

    print("\n=> " + ("全件 PASS" if failures == 0 else f"{failures} 件 FAIL"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
