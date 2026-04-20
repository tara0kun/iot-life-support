"""庭（ガーデン）モジュール。

毎日のスタンプ達成数を記録し、過去の花を庭として表示する。
達成度に応じて花の種類が変わり、日々の庭が成長していく。
"""
from __future__ import annotations

import json
from datetime import datetime, date, timedelta

from .db import get_conn, transaction

# 達成数 → 花の種類マッピング
FLOWER_TYPES = [
    {"key": "seed",    "emoji": "🫘",  "label": "たね"},
    {"key": "sprout",  "emoji": "🌱",  "label": "め"},
    {"key": "stem",    "emoji": "🌿",  "label": "くき"},
    {"key": "bud",     "emoji": "🌷",  "label": "つぼみ"},
    {"key": "flower",  "emoji": "🌸",  "label": "おはな"},
    {"key": "pretty",  "emoji": "🌺",  "label": "きれいなはな"},
    {"key": "big",     "emoji": "🌻",  "label": "おおきなはな"},
    {"key": "bouquet", "emoji": "💐",  "label": "まんかい"},
]


def _done_to_flower(done_count: int) -> dict:
    """達成数から花の種類を決定。"""
    idx = min(done_count, len(FLOWER_TYPES) - 1)
    return FLOWER_TYPES[idx]


def save_daily_score(
    person_id: int,
    target_date: date,
    done_count: int,
    total_count: int = 7,
    details: dict | None = None,
) -> None:
    """1日分のスコアを保存（UPSERT）。"""
    flower = _done_to_flower(done_count)
    date_str = target_date.isoformat()
    details_json = json.dumps(details, ensure_ascii=False) if details else None

    with transaction() as conn:
        conn.execute(
            """INSERT INTO daily_scores(person_id, date, done_count, total_count, flower_type, details)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(person_id, date) DO UPDATE SET
                 done_count = excluded.done_count,
                 total_count = excluded.total_count,
                 flower_type = excluded.flower_type,
                 details = excluded.details""",
            (person_id, date_str, done_count, total_count, flower["key"], details_json),
        )


def get_garden_data(person_id: int, days: int = 14) -> list[dict]:
    """過去N日分の庭データを取得。データがない日は seed として返す。"""
    conn = get_conn()
    try:
        end_date = date.today()
        start_date = end_date - timedelta(days=days - 1)

        rows = conn.execute(
            """SELECT date, done_count, total_count, flower_type, details
                 FROM daily_scores
                WHERE person_id = ? AND date >= ? AND date <= ?
                ORDER BY date""",
            (person_id, start_date.isoformat(), end_date.isoformat()),
        ).fetchall()

        # dict化
        scores_by_date = {}
        for r in rows:
            scores_by_date[r["date"]] = {
                "date": r["date"],
                "done_count": r["done_count"],
                "total_count": r["total_count"],
                "flower_type": r["flower_type"],
                "details": json.loads(r["details"]) if r["details"] else None,
            }

        # 全日分を埋める
        garden = []
        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            if date_str in scores_by_date:
                entry = scores_by_date[date_str]
            else:
                entry = {
                    "date": date_str,
                    "done_count": 0,
                    "total_count": 7,
                    "flower_type": "seed",
                    "details": None,
                }
            # emoji を付加
            flower_info = next(
                (f for f in FLOWER_TYPES if f["key"] == entry["flower_type"]),
                FLOWER_TYPES[0],
            )
            entry["emoji"] = flower_info["emoji"]
            entry["flower_label"] = flower_info["label"]
            # 日付の表示用
            entry["day"] = current.day
            entry["weekday"] = ["月", "火", "水", "木", "金", "土", "日"][current.weekday()]
            entry["is_today"] = current == end_date
            garden.append(entry)
            current += timedelta(days=1)

        return garden
    finally:
        conn.close()
