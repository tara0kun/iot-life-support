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

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable

from .db import get_conn, transaction

log = logging.getLogger("sessions")

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
MEAL_LABELS_FOR_MERGE = {"朝食", "昼食", "夕食", "間食", "夜食", "おやつ"}
MERGE_WINDOW_MINUTES = 60


def _load_unassigned_events(conn, since: datetime) -> list[EventRow]:
    # クラスタリングから除外:
    # - family_report / tablet_report / family_override: 手動記録なので集約対象外
    # - bathroom_meter (reading): 10秒おきに発火、クラスタが永久に切れずモンスター化する
    # - bath_motion: 頻発するセンサー、bath_detector で独立処理
    # - camera (person_detected): 頻発、クラスタリング上のノイズ
    cur = conn.execute(
        """
        SELECT e.id, e.person_id, e.source, e.event_type, e.started_at, e.ended_at, e.value
          FROM events e
          LEFT JOIN session_events se ON se.event_id = e.id
         WHERE se.event_id IS NULL
           AND e.started_at >= ?
           AND e.source NOT IN (
               'family_report', 'tablet_report', 'family_override',
               'bathroom_meter', 'bath_motion', 'camera'
           )
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


def _is_rice_lid_in_use(conn) -> bool:
    """過去24時間以内に rice_cooker_lid のイベントが1件でもあれば True。

    蓋センサーが運用中（設置済み・稼働中）の判定。
    True なら炊飯器単独の power_on は食事と認めず、蓋開イベントが必要になる。
    """
    try:
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            "SELECT 1 FROM events WHERE source = 'rice_cooker_lid' AND started_at >= ? LIMIT 1",
            (since,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _has_rice_lid_open(events: list[EventRow]) -> bool:
    """セッションのイベント群に rice_cooker_lid の open があるか。"""
    return any(
        e.source == "rice_cooker_lid" and e.event_type == "open" for e in events
    )


def _qualifies_as_session(events: list[EventRow], conn=None) -> bool:
    """食事セッションまたはお風呂セッションとして有効か。

    炊飯器/IH の単独 power_on は、蓋開イベントが同セッション内にない限り
    食事として認めない（保温パルス・蓋開時の温度補正反応を除外）。
    bath_motion / bathroom_meter は単独では食事の判定材料に含めない
    （脱衣所モーションが多発してメガクラスターになる問題対策）。
    """
    sources = {e.source for e in events}

    # お風呂セッション: bath_end イベントがあれば有効
    if _is_bath_session(events):
        return any(e.event_type == "bath_end" for e in events)

    # power_off 単独は食事とみなさない
    event_types = {e.event_type for e in events}
    if event_types == {"power_off"}:
        return False

    # 蓋開 + 炊飯器/IH → 確実に食事
    if _has_rice_lid_open(events) and ("rice_cooker" in sources or "ih" in sources):
        return True

    # 炊飯器/IH 単独 → 蓋開なしなので食事とは認めない
    # （他センサーとの組合せでなら下のルールで判定）
    if sources & {"rice_cooker", "ih"} and not _has_rice_lid_open(events):
        if len(sources - {"camera", "rice_cooker", "ih"}) == 0:
            # 他センサーゼロ → 食事ではない（保温パルス・蓋開反応・炊飯中）
            return False

    # 食事の判定材料から除外するソース:
    #   - bath_motion: 脱衣所モーション、頻発するため単独では食事と関係ない
    #   - bathroom_meter: 湿度センサー、定期受信のためノイズ
    #   - toilet_door: トイレは食事と無関係
    # これらが含まれていても、他の食事関連ソースと併発しなければ食事ではない
    food_relevant_sources = sources - {"camera", "bath_motion", "bathroom_meter", "toilet_door"}

    # 時間帯チェック: 深夜(22-5時)は「炊飯器の蓋開」必須で誤検知を厳しく抑制
    first_ts = min((e.started_at for e in events if e.started_at), default=None)
    is_late_night = first_ts is not None and (first_ts.hour >= 22 or first_ts.hour < 5)
    if is_late_night and not _has_rice_lid_open(events):
        # 深夜は冷蔵庫の水分補給などが多いので、蓋開がない限り食事と認めない
        return False

    # 蓋開センサー（rice_cooker_lid）が含まれて、かつ他の食事関連ソースもある → 食事
    if "rice_cooker_lid" in food_relevant_sources and len(food_relevant_sources) >= 2:
        return True

    # 蓋開がない場合: 食事関連ソース3種類以上を要求（厳しめ）
    # 例: 冷蔵庫 + 炊飯器電力 + 何か = 食事候補（ただし蓋開がないと弱い）
    if len(food_relevant_sources) >= 3:
        return True

    # 冷蔵庫単独や 2種類だけ（蓋開なし）は食事として認めない
    # （ユーザー要望: 冷蔵庫だけで食事判定しない）

    # カメラ単独 → 滞在時間で判定
    if sources == {"camera"}:
        total = 0.0
        for e in events:
            if e.event_type == "presence" and e.ended_at:
                total += (e.ended_at - e.started_at).total_seconds()
        return total >= MIN_PRESENCE_SECONDS
    return False


def _merge_close_meal_sessions(conn, lookback_hours: int = 6) -> int:
    """同じ人物の食事ラベルセッションが MERGE_WINDOW_MINUTES 以内に並んでいたら統合する。

    認知症で短時間に何度も炊飯器を操作するパターンを1食事として扱うための後処理。
    person_id=0（未確定）は同じ未確定同士でのみ統合（人物確定済みのセッションとは混ぜない）。

    戻り値: 統合した数（吸収された側のセッション数）。
    """
    since = datetime.now() - timedelta(hours=lookback_hours)
    placeholders = ",".join("?" * len(MEAL_LABELS_FOR_MERGE))
    rows = conn.execute(
        f"""SELECT id, person_id, started_at, ended_at, event_count, label
              FROM meal_sessions
             WHERE started_at >= ? AND label IN ({placeholders})
             ORDER BY person_id, started_at""",
        (since, *MEAL_LABELS_FOR_MERGE),
    ).fetchall()
    by_person: dict[int, list[dict]] = {}
    for r in rows:
        by_person.setdefault(r["person_id"], []).append(dict(r))

    merged = 0
    gap_threshold = timedelta(minutes=MERGE_WINDOW_MINUTES)
    for person_id, sess_list in by_person.items():
        sess_list.sort(key=lambda s: _to_dt(s["started_at"]))
        i = 0
        while i < len(sess_list) - 1:
            a = sess_list[i]
            b = sess_list[i + 1]
            a_end = _to_dt(a["ended_at"])
            b_start = _to_dt(b["started_at"])
            if (b_start - a_end) <= gap_threshold:
                # bをaに吸収
                b_end = _to_dt(b["ended_at"])
                new_end = max(a_end, b_end)
                new_count = (a["event_count"] or 0) + (b["event_count"] or 0)
                conn.execute(
                    "UPDATE meal_sessions SET ended_at = ?, event_count = ? WHERE id = ?",
                    (new_end, new_count, a["id"]),
                )
                # session_eventsを移動（PK衝突は無視）
                conn.execute(
                    "UPDATE OR IGNORE session_events SET session_id = ? WHERE session_id = ?",
                    (a["id"], b["id"]),
                )
                conn.execute(
                    "DELETE FROM session_events WHERE session_id = ?", (b["id"],)
                )
                conn.execute("DELETE FROM meal_sessions WHERE id = ?", (b["id"],))
                a["ended_at"] = new_end
                a["event_count"] = new_count
                sess_list.pop(i + 1)
                merged += 1
            else:
                i += 1
    return merged


def merge_sessions_manual(new_session_id: int, prev_session_id: int) -> bool:
    """手動でセッションを統合する（LINE「前と同じ食事」ボタン用）。

    new_session を prev_session に吸収させる。person_id は prev のものを優先、
    prev が未確定で new が確定済みなら new のものを採用。
    """
    if new_session_id == prev_session_id:
        return False
    with transaction() as conn:
        prev = conn.execute(
            "SELECT id, person_id, started_at, ended_at, event_count FROM meal_sessions WHERE id = ?",
            (prev_session_id,),
        ).fetchone()
        new = conn.execute(
            "SELECT id, person_id, started_at, ended_at, event_count FROM meal_sessions WHERE id = ?",
            (new_session_id,),
        ).fetchone()
        if not prev or not new:
            return False
        # 統合後の person_id（確定済みを優先）
        target_person = prev["person_id"] if prev["person_id"] != 0 else new["person_id"]
        # 時刻範囲を統合
        merged_start = min(_to_dt(prev["started_at"]), _to_dt(new["started_at"]))
        merged_end = max(_to_dt(prev["ended_at"]), _to_dt(new["ended_at"]))
        merged_count = (prev["event_count"] or 0) + (new["event_count"] or 0)
        conn.execute(
            "UPDATE meal_sessions SET person_id = ?, started_at = ?, ended_at = ?, event_count = ? WHERE id = ?",
            (target_person, merged_start, merged_end, merged_count, prev["id"]),
        )
        conn.execute(
            "UPDATE OR IGNORE session_events SET session_id = ? WHERE session_id = ?",
            (prev["id"], new["id"]),
        )
        conn.execute("DELETE FROM session_events WHERE session_id = ?", (new["id"],))
        # newに紐づくeventsもtarget_personで上書き（必要に応じて）
        conn.execute(
            """UPDATE events SET person_id = ?
               WHERE id IN (SELECT event_id FROM session_events WHERE session_id = ?)""",
            (target_person, prev["id"]),
        )
        conn.execute("DELETE FROM meal_sessions WHERE id = ?", (new["id"],))
    return True


def aggregate_sessions(lookback_hours: int = 24) -> list[int]:
    """未割当イベントをセッションにまとめる。戻り値=新規作成セッションIDのリスト。

    新規セッションは confirmed=0（未確定）で作成される。
    呼び出し側は各IDに対して LINE 確認プロンプトを送信する責務を持つ。
    """
    since = datetime.now() - timedelta(hours=lookback_hours)
    created_ids: list[int] = []

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
                if not _qualifies_as_session(cluster, conn=conn):
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
                # 未確定セッション (confirmed=0) として作成。
                # LINE で家族に確認 → handle_session_confirm_postback で confirmed=1 にする
                cur = conn.execute(
                    """INSERT INTO meal_sessions(person_id, started_at, ended_at,
                                                 event_count, label, confirmed)
                       VALUES(?, ?, ?, ?, ?, 0)""",
                    (person_id, started, ended, len(cluster), label),
                )
                sid = cur.lastrowid
                conn.executemany(
                    "INSERT INTO session_events(session_id, event_id) VALUES(?, ?)",
                    [(sid, c.id) for c in cluster],
                )
                created_ids.append(sid)

        # 近接食事セッションの自動統合（認知症パターン対策）
        merged_count = _merge_close_meal_sessions(conn)
        if merged_count:
            log.info("近接食事セッションを統合: %d件", merged_count)

    return created_ids


def sessions_today(person_id: int, include_unconfirmed: bool = False) -> list[dict]:
    """その日の食事セッション。デフォルトは confirmed=1 のみ（家族確認済）。"""
    today_start = datetime.combine(datetime.now().date(), time.min)
    conn = get_conn()
    try:
        if include_unconfirmed:
            rows = conn.execute(
                """SELECT id, started_at, ended_at, label, event_count, confirmed
                     FROM meal_sessions
                    WHERE person_id = ? AND started_at >= ?
                      AND confirmed != -1
                    ORDER BY started_at""",
                (person_id, today_start),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, started_at, ended_at, label, event_count
                     FROM meal_sessions
                    WHERE person_id = ? AND started_at >= ?
                      AND confirmed = 1
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
    """最後の確定済セッションを返す。"""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT id, started_at, ended_at, label
                 FROM meal_sessions
                WHERE confirmed = 1
                AND person_id = ?
                ORDER BY started_at DESC LIMIT 1""",
            (person_id,),
        ).fetchone()
        if row:
            d = dict(row)
            d["started_at"] = _to_dt(d["started_at"])
            return d
        return None
    finally:
        conn.close()

