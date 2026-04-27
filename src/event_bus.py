"""イベントバス: 各センサーからのイベントをDBに記録し、セッション集約を行う。

全センサーモジュールはこのバスにイベントを投げる。
バスはDBへの永続化とリアルタイム通知（WebSocket）を担当する。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from .db import get_conn, transaction

log = logging.getLogger("event_bus")

_listeners: list[asyncio.Queue] = []


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _listeners.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _listeners:
        _listeners.remove(q)


async def _notify(event: dict) -> None:
    for q in _listeners:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def record_event(
    source: str,
    event_type: str,
    person_id: int | None = None,
    value: float | None = None,
    confidence: float = 1.0,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    raw_meta: str | None = None,
) -> int:
    now = started_at or datetime.now()
    with transaction() as conn:
        cur = conn.execute(
            """INSERT INTO events(person_id, source, event_type, started_at,
                                  ended_at, value, confidence, raw_meta)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (person_id, source, event_type, now, ended_at, value, confidence, raw_meta),
        )
        event_id = cur.lastrowid

    event = {
        "id": event_id,
        "person_id": person_id,
        "source": source,
        "event_type": event_type,
        "started_at": now.isoformat(),
        "value": value,
    }
    log.info("EVENT: %s/%s person=%s value=%s", source, event_type, person_id, value)
    await _notify(event)
    return event_id


def get_events_today(person_id: int | None = None) -> list[dict]:
    today_start = datetime.combine(datetime.now().date(), datetime.min.time())
    conn = get_conn()
    try:
        if person_id:
            rows = conn.execute(
                """SELECT id, person_id, source, event_type, started_at, value
                     FROM events WHERE started_at >= ? AND person_id = ?
                     ORDER BY started_at DESC""",
                (today_start, person_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, person_id, source, event_type, started_at, value
                     FROM events WHERE started_at >= ?
                     ORDER BY started_at DESC""",
                (today_start,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_events(limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT e.id, e.person_id, p.name as person_name,
                      e.source, e.event_type, e.started_at, e.value
                 FROM events e
                 LEFT JOIN persons p ON p.id = e.person_id
                 ORDER BY e.started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_by_date(target_date: str, limit: int = 200) -> list[dict]:
    """指定日のイベントを取得する。target_date は 'YYYY-MM-DD' 形式。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT e.id, e.person_id, p.name as person_name,
                      e.source, e.event_type, e.started_at, e.value
                 FROM events e
                 LEFT JOIN persons p ON p.id = e.person_id
                 WHERE date(e.started_at) = ?
                 ORDER BY e.started_at DESC LIMIT ?""",
            (target_date, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
