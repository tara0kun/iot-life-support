"""食事・入浴セッション判定 (src/sessions.py) の回帰テスト。

ここは祖母のタブレットの「今日 N 回食べました」と、炊飯器ロックの起点
(lock_manager.should_warn_recent_meal → last_meal_session) の両方を支えている。
壊れると、認知症の祖母に嘘の食事回数が提示され、炊飯器が誤ってロックされる。
祖母は提示された内容を確認できないので、嘘がそのまま行動に影響する。

過去に実際に起きたこと（各テストの docstring に対応する事象を書いた）:
- 2026-09-21: 入浴のセッションが食事ラベルに化け、祖母のタブレットに
  実際には取っていない食事が表示された（30日中24日が該当）
- 2026-09-25 まで: 冷蔵庫や炊飯器の蓋開を含むクラスタが「失敗した入浴」として
  捨てられ、食事が消えていた（120日で 125/2045 クラスタ、うち約68件は蓋開を含む）
- 炊飯器ロックが 2026-08-09 以降まったく発火しなかった
- 2026-09-25 の修正は「ユニットテストが全部通った」とされながら回帰を4件出した

DB を触らない純粋な判定テストにも `seeded_db` を付けてある。判定関数が将来
`conn` / `get_conn()` を実際に使い始めても、本番 DB ではなく一時 DB に向くようにするため。
"""
from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime, time, timedelta

from src.sessions import (
    BATH_NEUTRAL_SOURCES,
    MEAL_LABELS,
    MIN_PRESENCE_SECONDS,
    EventRow,
    _guess_label,
    _is_bath_session,
    _qualifies_as_session,
    aggregate_sessions,
    last_meal_session,
    last_session,
    sessions_today,
)

# 深夜判定 (22時〜5時) に引っかからない基準時刻。
# _qualifies_as_session / _is_bath_session はイベントの時刻しか見ず now() を
# 読まないので、純粋な判定テストでは日付を固定できる。
NOON = datetime(2026, 10, 15, 12, 0, 0)

# 台所に実在するセンサー。ih と contact_sensor は sessions.py の判定ロジックに
# 名前が出てくるが、events に書き込む経路がどこにも無い（未設置）。
KITCHEN_SOURCES = ("fridge", "rice_cooker", "rice_cooker_lid")

# 食事判定から除外されるソース。クラスタに混ざっても食事関連ソース数を増やさない。
NON_FOOD_NOISE = ("camera", "toilet_door", "bath_motion", "bathroom_meter")


# --------------------------------------------------------------------------
# ローカルヘルパー（conftest.py は共有なので触らない）
# --------------------------------------------------------------------------
def _today_at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """今日の JST 時刻。

    aggregate_sessions / sessions_today は now() を基準に 24時間 / 当日0時で
    絞るため、固定日付ではイベントが拾われない。今日に寄せる必要がある。
    """
    return datetime.combine(datetime.now().date(), time(hour, minute, second))


def _fmt(dt: datetime) -> str:
    """本番と同じ格納形式（JST・文字列）に揃える。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ev(
    source: str,
    event_type: str = "open",
    at: datetime | None = None,
    ended_at: datetime | None = None,
    ev_id: int = 1,
) -> EventRow:
    """判定関数に直接渡す架空のイベント行。"""
    return EventRow(
        id=ev_id,
        person_id=1,
        source=source,
        event_type=event_type,
        started_at=at or NOON,
        ended_at=ended_at,
        value=None,
    )


def _add_events(db, rows, person_id: int = 1) -> list[int]:
    """events に架空のイベントを入れる。rows=[(source, event_type, started_at, ended_at)]"""
    conn = sqlite3.connect(db)
    ids: list[int] = []
    try:
        for source, event_type, started, ended in rows:
            cur = conn.execute(
                """INSERT INTO events(person_id, source, event_type, started_at, ended_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (person_id, source, event_type, _fmt(started), _fmt(ended) if ended else None),
            )
            ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return ids


def _add_session(
    db,
    started: datetime,
    label: str,
    confirmed: int = 1,
    person_id: int = 1,
    ended: datetime | None = None,
) -> int:
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            """INSERT INTO meal_sessions(person_id, started_at, ended_at,
                                         event_count, label, confirmed)
               VALUES(?, ?, ?, 1, ?, ?)""",
            (
                person_id,
                _fmt(started),
                _fmt(ended or started + timedelta(minutes=10)),
                label,
                confirmed,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _all_sessions(db) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT id, person_id, label, started_at, ended_at, event_count, confirmed
                     FROM meal_sessions ORDER BY started_at"""
            )
        ]
    finally:
        conn.close()


def _assigned_event_ids(db) -> set[int]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT event_id FROM session_events")}
    finally:
        conn.close()


# ==========================================================================
# 1. sessions_today(): お風呂が「間食」に化けないこと
# ==========================================================================
def test_お風呂セッションが2本目以降でも間食に化けない(seeded_db):
    """2026-09-25 まで、同じラベルの2本目以降を **無条件で** 「間食」に寄せていた。

    そのため1日に複数回検知された入浴の2本目が「間食」になり、
      - 祖母のタブレットに「間食を◯分前に食べました」と嘘が出る
      - 日次サマリの食事回数が入浴ぶん水増しされる
      - 食べ過ぎアラートと炊飯器ロック確認が入浴で発火しうる
    という害が出ていた。食事ラベル同士の重複だけが「間食」に寄るのが正しい。
    """
    _add_session(seeded_db, _today_at(7, 30), "朝食")
    _add_session(seeded_db, _today_at(12, 0), "昼食")
    _add_session(seeded_db, _today_at(13, 0), "昼食")  # 食事ラベルの重複 → 間食に寄る
    _add_session(seeded_db, _today_at(15, 0), "お風呂", confirmed=0)
    _add_session(seeded_db, _today_at(19, 30), "お風呂", confirmed=0)

    labels = [s["label"] for s in sessions_today(1)]
    assert labels == ["朝食", "昼食", "間食", "お風呂", "お風呂"]

    meals = [lb for lb in labels if lb in MEAL_LABELS]
    assert len(meals) == 3, f"入浴が食事回数に混ざった: {labels}"


def test_入浴だけの日の食事回数はゼロ(seeded_db):
    """2026-09-21 の事象の再現。入浴のセッションが食事ラベルに化けていた。

    食事していない日に「4回食べた」と出ると、家族は様子を見に行かず、
    祖母本人も「もう食べた」と思って食べない。実害が最も直接的なケース。
    """
    for hour in (9, 11, 14, 17, 20):
        _add_session(seeded_db, _today_at(hour, 0), "お風呂", confirmed=0)

    rows = sessions_today(1)
    assert len(rows) == 5
    assert {r["label"] for r in rows} == {"お風呂"}
    assert [r for r in rows if r["label"] in MEAL_LABELS] == []


def test_お風呂は家族確認がなくても実績に数える(seeded_db):
    """お風呂は LINE 確認フローを意図的にスキップするので confirmed=1 になる経路が無い。

    既定の「確定済のみ」で絞っていた間、18時の check_bath() が毎日「未入浴」と
    誤リマインドし、22時の日次サマリでもお風呂が永久に未実施だった。
    一方で **未確定の食事は出してはいけない**（食べたと断定できないため）。
    """
    _add_session(seeded_db, _today_at(12, 0), "昼食", confirmed=0)
    _add_session(seeded_db, _today_at(19, 0), "お風呂", confirmed=0)

    assert [s["label"] for s in sessions_today(1)] == ["お風呂"]


def test_却下されたお風呂は実績に数えない(seeded_db):
    """家族が誤検知として却下 (confirmed=-1) したものは実績から外れること。

    却下が効かないと、湯はりや清掃を「入浴した」と数えて入浴リマインドが止まる。
    """
    _add_session(seeded_db, _today_at(19, 0), "お風呂", confirmed=-1)
    assert sessions_today(1) == []


def test_未確定の食事はinclude_unconfirmedでだけ出てくる(seeded_db):
    """家族UIは未確定も見たい／祖母のタブレットは確定済だけ、という2つの用途がある。

    既定が未確定込みに変わると、祖母に「食べましたよ」と未確認の推測を見せることになる。
    """
    _add_session(seeded_db, _today_at(12, 0), "昼食", confirmed=0)

    assert [s["label"] for s in sessions_today(1)] == []
    assert [s["label"] for s in sessions_today(1, include_unconfirmed=True)] == ["昼食"]


def test_前日のセッションは今日の実績に入らない(seeded_db):
    """日付の切り方が崩れると食事回数が日をまたいで累積する。"""
    _add_session(seeded_db, _today_at(20, 0) - timedelta(days=1), "夕食")
    _add_session(seeded_db, _today_at(12, 0), "昼食")

    assert [s["label"] for s in sessions_today(1)] == ["昼食"]


# ==========================================================================
# 2. _is_bath_session(): 除外リスト
# ==========================================================================
def test_お風呂関連ソースだけならお風呂セッション(seeded_db):
    """中立ソース（カメラ・湿度計・トイレドア）が混ざっても入浴判定は生きること。

    トイレは入浴の前後に行くのが自然なので、お風呂を否定してはいけない。
    """
    assert _is_bath_session([_ev("bath_door", "bath_start"), _ev("bath_door", "bath_end")])
    assert _is_bath_session(
        [
            _ev("bath_door", "bath_start"),
            _ev("bath_motion", "motion"),
            _ev("camera", "person_detected"),
            _ev("toilet_door", "open"),
            _ev("bathroom_meter", "reading"),
        ]
    )


def test_冷蔵庫が混ざったクラスタはお風呂ではない(seeded_db):
    """2026-09-25 まで除外リストに fridge が入っていなかった。

    「浴室ドア + 冷蔵庫」がお風呂と判定され、bath_end が無いので
    _qualifies_as_session で丸ごと捨てられていた。**食事が消えていた。**
    """
    assert not _is_bath_session([_ev("bath_door", "bath_start"), _ev("fridge", "open")])


def test_炊飯器の蓋開が混ざったクラスタはお風呂ではない(seeded_db):
    """同じく rice_cooker_lid も除外リストに無かった。

    蓋開を含むクラスタは食事として成立すべきもの（約68件）だったのに、
    「失敗した入浴」として捨てられていた。
    """
    assert not _is_bath_session(
        [_ev("bath_door", "bath_start"), _ev("rice_cooker_lid", "open")]
    )
    assert not _is_bath_session(
        [
            _ev("bath_door", "bath_end"),
            _ev("rice_cooker_lid", "open"),
            _ev("rice_cooker", "power_on"),
        ]
    )


def test_お風呂判定の中立ソースに台所のセンサーは入っていない(seeded_db):
    """除外し忘れの再発防止を集合レベルで固定する。

    BATH_NEUTRAL_SOURCES に台所センサーを足すと、食事を含むクラスタが
    再び「失敗した入浴」として捨てられる。個別ケースのテストだけだと
    新しいソースを足したときに見逃すので、集合そのものを見る。
    """
    leaked = BATH_NEUTRAL_SOURCES & (set(KITCHEN_SOURCES) | {"ih"})
    assert not leaked, f"台所センサーがお風呂の中立扱いになっている: {leaked}"


def test_浴室ドアと蓋開が混ざったクラスタは食事として残る(seeded_db):
    """除外リスト修正の全経路テスト（単体判定だけでなく aggregate まで通す）。

    入浴直後に夕飯を炊く、という祖母の実際の生活パターンがこれに当たる。
    ここが壊れると食事そのものが記録から消え、安否判定も炊飯器ロックも動かない。
    """
    base = _today_at(18, 30)
    _add_events(
        seeded_db,
        [
            ("bath_door", "bath_end", base, None),
            ("rice_cooker_lid", "open", base + timedelta(minutes=3), None),
            ("rice_cooker", "power_on", base + timedelta(minutes=5), None),
        ],
    )

    aggregate_sessions()
    rows = _all_sessions(seeded_db)
    assert len(rows) == 1, f"クラスタが捨てられた: {rows}"
    assert rows[0]["label"] == "夕食", "お風呂として捨てられてはいけない"
    assert rows[0]["event_count"] == 3


# ==========================================================================
# 3. _qualifies_as_session(): 成立規則を1つずつ
# ==========================================================================
def test_蓋開と炊飯器があれば食事(seeded_db):
    """最も確実な食事の証拠。これが成立しないと食事検知が全面的に止まる。"""
    assert _qualifies_as_session(
        [_ev("rice_cooker_lid", "open"), _ev("rice_cooker", "power_on")]
    )


def test_蓋開と冷蔵庫の2種でも食事(seeded_db):
    """蓋開が含まれていれば、食事関連ソース2種で成立する（規則が緩む側）。"""
    assert _qualifies_as_session([_ev("rice_cooker_lid", "open"), _ev("fridge", "open")])


def test_炊飯器単独の通電は食事ではない(seeded_db):
    """保温パルスと、蓋開時の温度補正による通電を食事にしないため。

    これを食事にすると、炊飯器が保温しているだけで「食べた」ことになり、
    祖母のタブレットに嘘が出る。
    """
    assert not _qualifies_as_session(
        [
            _ev("rice_cooker", "power_on", NOON),
            _ev("rice_cooker", "power_on", NOON + timedelta(minutes=2)),
        ]
    )


def test_蓋開だけでは食事にならない(seeded_db):
    """蓋開は単独では成立せず、食事関連ソース2種以上が必要。

    蓋を開けて閉めただけ（中身を確認した、ご飯の量を見た）を食事にしないため。
    """
    assert not _qualifies_as_session([_ev("rice_cooker_lid", "open")])


def test_食事関連ソース3種以上なら蓋開なしでも食事(seeded_db):
    """蓋開が無い場合の唯一の成立規則（食事関連ソース3種以上）。

    同時に、トイレ・カメラ・脱衣所モーション・湿度計はいくら混ざっても
    この3種に数えられないことを確認する。数えてしまうと、トイレに行って
    カメラに映っただけで「食事」になる。
    """
    assert _qualifies_as_session(
        [_ev("fridge", "open"), _ev("rice_cooker", "power_on"), _ev("ih", "power_on")]
    )
    assert not _qualifies_as_session(
        [_ev("fridge", "open")] + [_ev(s, "reading") for s in NON_FOOD_NOISE]
    )


def test_蓋センサーが無い構成では食事が成立しない(seeded_db):
    """**仕様の確認。実装を変えるためのテストではない。**

    台所に実在するのは fridge / rice_cooker / rice_cooker_lid の3つだけ
    （ih と contact_sensor は判定コードに名前があるだけで未設置）。
    蓋開を含まない成立規則は「食事関連ソース3種以上」しか無いので、
    蓋センサーが落ちると台所からはどう組み合わせても食事が成立しない。
    蓋センサーは 2026-09-23 から故障しており、食事検知と炊飯器ロックの
    起点が供給されない状態が続いている。

    このテストが落ちたら「蓋なしで食事が成立するようになった」ということ。
    意図した変更なら、炊飯器ロックの誤発火を確認した上でこのテストを書き換える。
    """
    surviving = [s for s in KITCHEN_SOURCES if s != "rice_cooker_lid"]
    noise = [_ev(s, "reading") for s in NON_FOOD_NOISE]
    for n in (1, 2):
        for combo in itertools.combinations(surviving, n):
            events = [
                _ev(s, "open" if s == "fridge" else "power_on", NOON + timedelta(minutes=i))
                for i, s in enumerate(combo)
            ]
            assert not _qualifies_as_session(events), f"{combo} だけで食事が成立した"
            assert not _qualifies_as_session(events + noise), (
                f"{combo} + 非食事ソースで食事が成立した"
            )


def test_冷蔵庫と炊飯器だけの一日は食事が1件も作られない(seeded_db):
    """上の仕様を aggregate_sessions の全経路で確認する。

    炊飯器ロックが 2026-08-09 以降発火しなかったのはこの連鎖によるもの。
    セッションが作られない → confirmed=1 の食事が無い → last_meal_session が
    None → should_warn_recent_meal が常に None → ロックが一度も起きない。
    """
    base = _today_at(12, 0)
    _add_events(
        seeded_db,
        [
            ("fridge", "open", base, None),
            ("rice_cooker", "power_on", base + timedelta(minutes=2), None),
            ("fridge", "open", base + timedelta(minutes=5), None),
        ],
    )

    assert aggregate_sessions() == []
    assert _all_sessions(seeded_db) == []


def test_蓋センサーの稼働状況は判定に影響しない(seeded_db):
    """`_is_rice_lid_in_use()` は定義されているが呼び出し元がゼロ（死にコード）。

    その docstring は「蓋センサー運用中なら炊飯器単独を食事と認めない」と
    条件付きに読めるが、実装は蓋センサーの設置状況に関係なく常に不成立。
    つまり蓋センサーが壊れたときに判定が緩む救済は存在しない。
    `conn` 引数も _qualifies_as_session の本体では一切参照されない。
    この挙動を知らずに「蓋が無い間は冷蔵庫＋炊飯器で代替されるはず」と
    考えると、食事が検知されない理由を見失う。
    """
    from src.db import get_conn

    events = [_ev("rice_cooker", "power_on"), _ev("fridge", "open")]
    conn = get_conn()
    try:
        without_lid_history = _qualifies_as_session(events, conn=conn)
        conn.execute(
            """INSERT INTO events(person_id, source, event_type, started_at)
               VALUES(1, 'rice_cooker_lid', 'open', ?)""",
            (_fmt(datetime.now()),),
        )
        conn.commit()
        with_lid_history = _qualifies_as_session(events, conn=conn)
    finally:
        conn.close()

    assert without_lid_history is False
    assert with_lid_history is False


def test_深夜は蓋開がなければ食事にしない(seeded_db):
    """深夜の冷蔵庫は水分補給が多いので、3種あっても蓋開がなければ食事にしない。

    夜中の水分補給を「夜食」として記録すると、翌朝の食事回数と
    安否判定（最終活動時刻の意味）が両方ずれる。
    """
    night = datetime(2026, 10, 15, 23, 30)
    assert not _qualifies_as_session(
        [
            _ev("fridge", "open", night),
            _ev("rice_cooker", "power_on", night),
            _ev("ih", "power_on", night),
        ]
    )
    # 蓋開があれば深夜でも食事（規則の順序として深夜チェックより前に通る）
    assert _qualifies_as_session(
        [_ev("rice_cooker_lid", "open", night), _ev("rice_cooker", "power_on", night)]
    )


def test_深夜判定の境界は22時と5時(seeded_db):
    """境界がずれると、夕食の時間帯や早朝の食事が丸ごと捨てられる。"""
    def three_sources(at: datetime) -> list[EventRow]:
        return [
            _ev("fridge", "open", at),
            _ev("rice_cooker", "power_on", at),
            _ev("ih", "power_on", at),
        ]

    wrong = []
    for (hour, minute), want in [
        ((21, 59), True),
        ((22, 0), False),
        ((4, 59), False),
        ((5, 0), True),
    ]:
        at = datetime(2026, 10, 15, hour, minute)
        got = _qualifies_as_session(three_sources(at))
        if got is not want:
            wrong.append(f"{hour:02d}:{minute:02d} -> 成立={got} (期待 {want})")
    assert not wrong, "深夜判定の境界がずれている:\n  " + "\n  ".join(wrong)


def test_電源オフだけのクラスタは食事ではない(seeded_db):
    """炊飯器を切った／IHを切っただけを食事にしないため。"""
    assert not _qualifies_as_session(
        [_ev("rice_cooker", "power_off"), _ev("ih", "power_off")]
    )


def test_お風呂はbath_endが無ければセッションにならない(seeded_db):
    """入り始めただけ（bath_start のみ）で入浴済みにすると、18時の入浴リマインドが
    出なくなる。逆に bath_end があれば必ず成立しなければ、毎日「未入浴」になる。
    """
    assert not _qualifies_as_session([_ev("bath_door", "bath_start")])
    assert _qualifies_as_session(
        [_ev("bath_door", "bath_start"), _ev("bath_door", "bath_end")]
    )


def test_カメラ単独は滞在時間で判定する(seeded_db):
    """MIN_PRESENCE_SECONDS の境界。

    ただし camera は _load_unassigned_events で除外されるため、この規則は
    aggregate_sessions からは現状到達しない（モジュール docstring の
    「カメラ presence で成立」は死んでいる）。規則自体の挙動だけ固定する。
    """
    start = NOON
    assert _qualifies_as_session(
        [_ev("camera", "presence", start, start + timedelta(seconds=MIN_PRESENCE_SECONDS))]
    )
    assert not _qualifies_as_session(
        [
            _ev(
                "camera",
                "presence",
                start,
                start + timedelta(seconds=MIN_PRESENCE_SECONDS - 1),
            )
        ]
    )


# ==========================================================================
# 4. last_meal_session(): 食事ラベルだけを返すこと
# ==========================================================================
def test_last_meal_sessionは食事ラベルだけを返す(seeded_db):
    """炊飯器ロックの起点。

    ラベルを問わない last_session() を使っていたため、
    should_warn_recent_meal() が「お風呂の直後」「家族が起床スタンプを押した直後」を
    『直近に食事した』と誤認し、入浴直後に炊飯器ロックが発火しうる状態だった。
    """
    _add_session(seeded_db, _today_at(6, 0), "起床")
    _add_session(seeded_db, _today_at(7, 30), "朝食")
    _add_session(seeded_db, _today_at(12, 0), "お薬")
    _add_session(seeded_db, _today_at(19, 0), "お風呂")
    _add_session(seeded_db, _today_at(21, 0), "就寝")

    last_meal = last_meal_session(1)
    assert last_meal is not None
    assert last_meal["label"] == "朝食"
    assert last_meal["started_at"] == _today_at(7, 30)

    # 対比: ラベルを問わない last_session は非食事ラベルを返す
    assert last_session(1)["label"] == "就寝"


def test_last_meal_sessionは未確定と却下を無視する(seeded_db):
    """未確定の推測でロックを掛けると、食べていない祖母が炊飯器を使えなくなる。"""
    _add_session(seeded_db, _today_at(7, 30), "朝食", confirmed=1)
    _add_session(seeded_db, _today_at(12, 0), "昼食", confirmed=0)
    _add_session(seeded_db, _today_at(18, 0), "夕食", confirmed=-1)

    assert last_meal_session(1)["label"] == "朝食"


def test_last_meal_sessionは他人の食事を拾わない(seeded_db):
    """家族（person_id=3）が食べた記録で祖母の炊飯器がロックされてはいけない。"""
    _add_session(seeded_db, _today_at(7, 30), "朝食", person_id=1)
    _add_session(seeded_db, _today_at(19, 0), "夕食", person_id=3)

    assert last_meal_session(1)["label"] == "朝食"
    assert last_meal_session(3)["label"] == "夕食"


def test_夜食とおやつも食事として扱う(seeded_db):
    """MEAL_LABELS と同等の集合がコード内に8箇所あり、中身が3通りに割れている。

    「夜食」を含むのは sessions.py だけ、「おやつ」を含まないものも2箇所ある。
    統合のときにここから落ちると、夜食やおやつの直後に炊飯器ロックが発火する。
    """
    assert {"夜食", "おやつ"} <= MEAL_LABELS

    _add_session(seeded_db, _today_at(22, 0), "就寝")  # 常に最後にある非食事ラベル
    for i, label in enumerate(sorted(MEAL_LABELS)):
        _add_session(seeded_db, _today_at(6 + i, 0), label)
        got = last_meal_session(1)
        assert got is not None and got["label"] == label, (
            f"{label} が食事として扱われていない"
        )


# ==========================================================================
# 5. クラスタリングのギャップ境界 (SESSION_GAP_MINUTES = 15)
# ==========================================================================
def _aggregate_with_gap(db, gap: timedelta) -> list[dict]:
    """「蓋開 + 炊飯器」の成立クラスタの後ろに、gap だけ離して冷蔵庫を1件置く。

    同じクラスタに入れば event_count=3、別クラスタなら冷蔵庫単独は食事に
    成立せず捨てられて event_count=2 になる。
    セッション数で見ないのは、_merge_close_meal_sessions の統合窓が60分あり、
    15分差の食事セッションは必ず統合されて1件に戻ってしまうため
    （境界はセッション数には現れない）。
    """
    base = _today_at(12, 0)
    rice_at = base + timedelta(minutes=1)
    _add_events(
        db,
        [
            ("rice_cooker_lid", "open", base, None),
            ("rice_cooker", "power_on", rice_at, None),
            ("fridge", "open", rice_at + gap, None),
        ],
    )
    aggregate_sessions()
    return _all_sessions(db)


def test_14分59秒差は同じセッションにまとまる(seeded_db):
    """しきい値を下げると、1回の食事が複数回に割れて食べ過ぎアラートが出る。"""
    sessions = _aggregate_with_gap(seeded_db, timedelta(minutes=14, seconds=59))
    assert len(sessions) == 1
    assert sessions[0]["event_count"] == 3
    assert len(_assigned_event_ids(seeded_db)) == 3


def test_15分ちょうどは同じセッション(seeded_db):
    """境界は「以下」で同一クラスタ（<= gap）。等号の扱いが変わると挙動が1件分ずれる。"""
    sessions = _aggregate_with_gap(seeded_db, timedelta(minutes=15))
    assert len(sessions) == 1
    assert sessions[0]["event_count"] == 3


def test_15分01秒差は別クラスタになる(seeded_db):
    """しきい値を上げると、昼の食事と夕方の冷蔵庫が1つの食事に溶けて回数が減る。"""
    sessions = _aggregate_with_gap(seeded_db, timedelta(minutes=15, seconds=1))
    assert len(sessions) == 1
    assert sessions[0]["event_count"] == 2
    assert len(_assigned_event_ids(seeded_db)) == 2, (
        "離れた冷蔵庫イベントは別クラスタで、単独では食事に成立しない"
    )


# ==========================================================================
# 6. ラベル決定（時刻帯 → 朝食/昼食/夕食）の境界
# ==========================================================================
def test_時刻帯ラベルの境界(seeded_db):
    """ラベルは祖母への提示文と日次サマリの食事区分に直結する。

    境界が1つずれると、夕食が「間食」になって夕食リマインドが止まる、
    朝食が「夜食」になる、といった形で表示と通知が同時に狂う。
    """
    wrong = []
    for (hour, minute), want in [
        ((4, 59), "夜食"),
        ((5, 0), "朝食"),
        ((10, 29), "朝食"),
        ((10, 30), "昼食"),
        ((14, 29), "昼食"),
        ((14, 30), "間食"),
        ((16, 59), "間食"),
        ((17, 0), "夕食"),
        ((21, 29), "夕食"),
        ((21, 30), "夜食"),
        ((23, 59), "夜食"),
    ]:
        got = _guess_label(datetime(2026, 10, 15, hour, minute))
        if got != want:
            wrong.append(f"{hour:02d}:{minute:02d} -> {got} (期待 {want})")
    assert not wrong, "時刻帯ラベルの境界がずれている:\n  " + "\n  ".join(wrong)


def test_集約セッションのラベルは開始時刻で決まる(seeded_db):
    """境界をまたぐクラスタは **開始時刻** のラベルになる（現仕様）。

    夕方に始まった2本目の食事セッションは「間食」として記録される。
    判定に使うのは開始時刻だけで、終了時刻や中間時刻を
    基準に変えると、過去の記録と日次サマリの食事区分が食い違うので、
    変えるなら意図的に変えること。
    """
    base = _today_at(16, 58)
    _add_events(
        seeded_db,
        [
            ("rice_cooker_lid", "open", base, None),
            ("rice_cooker", "power_on", base + timedelta(minutes=10), None),
        ],
    )

    aggregate_sessions()
    rows = _all_sessions(seeded_db)
    assert [r["label"] for r in rows] == ["間食"]


def test_17時台に始まった食事は夕食になる(seeded_db):
    """夕食が夕食として記録されること（全経路）。

    ここが崩れると「夕食を食べていない」ことになり、夕食リマインドと
    日次サマリが同時にずれる。
    """
    base = _today_at(17, 5)
    _add_events(
        seeded_db,
        [
            ("rice_cooker_lid", "open", base, None),
            ("rice_cooker", "power_on", base + timedelta(minutes=2), None),
        ],
    )

    aggregate_sessions()
    assert [r["label"] for r in _all_sessions(seeded_db)] == ["夕食"]
