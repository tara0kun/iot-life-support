"""食事セッション集約ロジック。

ルール:
- 同一人物の連続するイベントで、隣接イベント間隔が SESSION_GAP_MINUTES 以下なら
  同じ食事セッションにまとめる
- 食事セッションとみなす最低条件:
    - 異なる source が 2種類以上 (例: 冷蔵庫 + 炊飯器)
    - または カメラ presence で滞在時間が MIN_PRESENCE_SECONDS 以上
- 時間帯で label を自動推定 (朝食/昼食/夕食/間食)

※祖母への "前回食事" 表示はここで作る sessions を参照する。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable

from .db import get_conn, transaction

SESSION_GAP_MINUTES = 15
MIN_PRESENCE_SECONDS = 180


def _to_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        return datetime.fromisoformat(v)
    return datetime.now()


def _guess_label(t: datetime) -> str:
    tt = t.time()
    if time(5, 0) <= tt < time(10, 30):
        return "朝食"
    if time(10, 30) <= tt < time(14, 30):
        return "昼食"
    if time(14, 30) <= tt < time(17, 0):
        return "間食"
    if time(17, 0) <= tt < time(21, 30):
        return "夕食"
    return "夜食"


@dataclass
class EventRow:
    id: int
    person_id: int
    source: str
    event_type: str
    started_at: datetime
    ended_at: datetime | None
    value: float | None


UNASSIGNED_PERSON_ID = 0  # 未確定セッションの person_id


def _load_unassigned_events(conn, since: datetime) -> list[EventRow]:
    cur = conn.execute(
        """
        SELECT e.id, e.person_id, e.source, e.event_type, e.started_at, e.ended_at, e.value
          FROM events e
          LEFT JOIN session_events se ON se.event_id = e.id
         WHERE se.event_id IS NULL
           AND e.started_at >= ?
           AND e.source NOT IN ('family_report', 'tablet_report', 'family_override')
         ORDER BY COALESCE(e.person_id, 0), e.started_at
        """,
        (since,),
    )
    return [
        EventRow(
            id=r["id"],
            person_id=r["person_id"] if r["person_id"] is not None else UNASSIGNED_PERSON_ID,
            source=r["source"],
            event_type=r["event_type"],
            started_at=_to_dt(r["started_at"]),
            ended_at=_to_dt(r["ended_at"]) if r["ended_at"] else None,
            value=r["value"],
        )
        for r in cur.fetchall()
    ]


BATH_SOURCES = {"bath_door", "bath_motion"}


def _is_bath_session(events: list[EventRow]) -> bool:
    """お風呂関連イベントのみで構成されているか。"""
    sources = {e.source for e in events}
    return bool(sources & BATH_SOURCES) and not (sources & {"rice_cooker", "ih", "contact_sensor"})


def _qualifies_as_session(events: list[EventRow]) -> bool:
    """食事セッションまたはお風呂セッションとして有効か。"""
    sources = {e.source for e in events}

    # お風呂セッション: bath_end イベントがあれば有効
    if _is_bath_session(events):
        return any(e.event_type == "bath_end" for e in events)

    # 以下は食事セッション判定
    # power_off 単独は食事とみなさない（炊飯完了の残りイベント）
    event_types = {e.event_type for e in events}
    if event_types == {"power_off"}:
        return False

    if len(sources - {"camera"}) >= 2:
        return True
    if sources == {"fridge"} and len(events) >= 2:
        return True
    if sources & {"rice_cooker", "ih"}:
        return True
    if sources == {"camera"}:
        total = 0.0
        for e in events:
            if e.event_type == "presence" and e.ended_at:
                total += (e.ended_at - e.started_at).total_seconds()
        return total >= MIN_PRESENCE_SECONDS
    return False


def aggregate_sessions(lookback_hours: int = 24) -> int:
    """未割当イベントをセッションにまとめる。戻り値=新規作成セッション数。"""
    since = datetime.now() - timedelta(hours=lookback_hours)
    created = 0

    with transaction() as conn:
        events = _load_unassigned_events(conn, since)

        # person_id でグループ化し、時系列で隣接性を判定
        by_person: dict[int, list[EventRow]] = {}
        for e in events:
            by_person.setdefault(e.person_id, []).append(e)

        gap = timedelta(minutes=SESSION_GAP_MINUTES)

        for person_id, evs in by_person.items():
            evs.sort(key=lambda x: x.started_at)
            clusters: list[list[EventRow]] = []
            current: list[EventRow] = []

            for e in evs:
                if not current:
                    current = [e]
                    continue
                prev_end = current[-1].ended_at or current[-1].started_at
                if e.started_at - prev_end <= gap:
                    current.append(e)
                else:
                    clusters.append(current)
                    current = [e]
            if current:
                clusters.append(current)

            for cluster in clusters:
                if not _qualifies_as_session(cluster):
                    continue
                # お風呂セッションの場合は「お風呂」ラベル
                if _is_bath_session(cluster):
                    started = cluster[0].started_at
                    ended = max((c.ended_at or c.started_at) for c in cluster)
                    label = "お風呂"
                else:
                    # 食事関連イベントの時刻を基準にする（カメラのみの検知は除外）
                    food_events = [c for c in cluster if c.source != "camera"]
                    started = food_events[0].started_at if food_events else cluster[0].started_at
                    ended = max((c.ended_at or c.started_at) for c in cluster)
                    label = _guess_label(started)
                cur = conn.execute(
                    """INSERT INTO meal_sessions(person_id, started_at, ended_at,
                                                 event_count, label)
                       VALUES(?, ?, ?, ?, ?)""",
                    (person_id, started, ended, len(cluster), label),
                )
                sid = cur.lastrowid
                conn.executemany(
                    "INSERT INTO session_events(session_id, event_id) VALUES(?, ?)",
                    [(sid, c.id) for c in cluster],
                )
                created += 1

    return created


def sessions_today(person_id: int) -> list[dict]:
    today_start = datetime.combine(datetime.now().date(), time.min)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, started_at, ended_at, label, event_count
                 FROM meal_sessions
                WHERE person_id = ? AND started_at >= ?
                ORDER BY started_at""",
            (person_id, today_start),
        ).fetchall()
        results = []
        seen_labels = set()
        for r in rows:
            d = dict(r)
            d["started_at"] = _to_dt(d["started_at"])
            # 同じラベルのセッションは最初のものだけ（重複排除）
            if d["label"] in seen_labels:
                d["label"] = "間食"
            seen_labels.add(d["label"])
            results.append(d)
        return results
    finally:
        conn.close()


def last_session(person_id: int) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT id, started_at, ended_at, label
                 FROM meal_sessions
                WHERE person_id = ?
                ORDER BY started_at DESC LIMIT 1""",
            (person_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["started_at"] = _to_dt(d["started_at"])
        return d
    finally:
        conn.close()
