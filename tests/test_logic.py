"""炊飯器ロック・庭（花）・お風呂監視のロジック。

担当: `src/lock_manager.py` / `src/garden.py` / `src/bath_monitor.py`。

ここで守りたいのは「通ったら安心」ではなく「壊れたら気づける」こと。
このファイルのテストは全部、過去に実際に起きた事故か、起きたら祖母・家族に
直接害が出る箇所に対応している。

- 炊飯器ロックが 2026-08-09 から 2026-10-15 まで2ヶ月間一度も発火せず、
  誰も気づかなかった（判定が「食事カウントが増えたティック」の中にあり、
  カウントが増えるのは確定時＝食事開始から中央値7.6時間後なので、
  RECENT_MEAL_MINUTES=90 の窓が必ず閉じていた）
- 2026-08-30 の自動ロックが 09-01 の家族による手動解除まで50時間14分続いた
  （解除予約が in-process の sleep しか無く、プロセス再起動で消えた）
- 入浴や家族の手動スタンプが「直近の食事」と数えられ、炊飯器がロックされかけた
- 浴室アラート後にモーションが戻っても解決扱いにならず、再通知が止まらなかった
"""
from __future__ import annotations

import ast
import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GRANDMA_ID = 1          # persons.id=1（seeded_db では 'テスト祖母'）
OTHER_FAMILY_ID = 3     # persons.id=3（seeded_db では 'テスト家族'）


# ======================================================================
# ローカルヘルパ
# conftest.py は他のエージェントと共有なので触らない約束。
# このファイル専用の足場はここに置く。
# ======================================================================

def _insert_meal_session(
    db_path,
    started_at: datetime,
    label: str = "昼食",
    confirmed: int = 1,
    person_id: int = GRANDMA_ID,
    duration_minutes: int = 20,
    confirmed_at: datetime | None = None,
) -> int:
    """meal_sessions に1行入れて id を返す。

    started_at / ended_at は JST の naive、confirmed_at だけ UTC（スキーマの罠）。
    """
    ended_at = started_at + timedelta(minutes=duration_minutes)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO meal_sessions
                   (person_id, started_at, ended_at, event_count, label,
                    confirmed, confirmed_by, confirmed_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                person_id,
                started_at.strftime("%Y-%m-%d %H:%M:%S"),
                ended_at.strftime("%Y-%m-%d %H:%M:%S"),
                3,
                label,
                confirmed,
                "テスト家族のLINE" if confirmed else None,
                confirmed_at.strftime("%Y-%m-%d %H:%M:%S") if confirmed_at else None,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_device_state(
    db_path,
    device_name: str = "rice_cooker",
    is_locked: int = 1,
    unlock_due_at: str | None = None,
    last_cycle_at: str | None = None,
    updated_at: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO device_state
                   (device_name, is_locked, last_cycle_at, cycle_count_today,
                    updated_at, unlock_due_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (device_name, is_locked, last_cycle_at, 1,
             updated_at or last_cycle_at, unlock_due_at),
        )
        conn.commit()
    finally:
        conn.close()


def _device_row(db_path, device_name: str = "rice_cooker") -> dict:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM device_state WHERE device_name = ?", (device_name,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _sql_now(offset_minutes: float = 0.0, *, microseconds: bool = True) -> str:
    """lock_device が書くのと同じ形の JST タイムスタンプ文字列。

    本番は `datetime.now()` を sqlite3 のアダプタ経由で入れているので
    マイクロ秒6桁が付く。SQLite の `datetime()` がこの形を食えるかどうかが
    期限の自己修復の成否を分けるため、既定でマイクロ秒を付けたまま試す。
    """
    t = datetime.now() + timedelta(minutes=offset_minutes)
    return str(t) if microseconds else t.strftime("%Y-%m-%d %H:%M:%S")


# --- monitor.py を AST で読むためのヘルパ -----------------------------
# monitor.py は import すると logging が本番の logs/monitor.log を掴むので、
# import せずにソースを構文解析して構造だけを検査する。

def _parse_func(rel_path: str, name: str) -> ast.AST:
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            for parent in ast.walk(node):
                for child in ast.iter_child_nodes(parent):
                    child.parent = parent  # type: ignore[attr-defined]
            return node
    raise AssertionError(f"{rel_path} に {name} が見つからない（リネームされた？）")


def _enclosing_if_tests(root: ast.AST, target: ast.AST) -> list[str]:
    """target を包んでいる if 文の条件式を内側から順に返す。"""
    out: list[str] = []
    node = getattr(target, "parent", None)
    while node is not None and node is not root:
        if isinstance(node, ast.If):
            out.append(ast.unparse(node.test))
        node = getattr(node, "parent", None)
    return out


def _find_calls(root: ast.AST, func_name: str) -> list[ast.Call]:
    return [
        n for n in ast.walk(root)
        if isinstance(n, ast.Call)
        and ((isinstance(n.func, ast.Name) and n.func.id == func_name)
             or (isinstance(n.func, ast.Attribute) and n.func.attr == func_name))
    ]


# ======================================================================
# 1. should_warn_recent_meal() — 炊飯器ロック確認の起点
# ======================================================================

def test_直近90分以内の確定食事があれば警告を返す(seeded_db):
    """ロック確認の入口。ここが None を返し続けると炊飯器は永久に守られない。

    2026-08-09 から2ヶ月間、この関数が常に None を返していたことに
    気づけなかった（呼び出しタイミングが悪く、窓が必ず閉じていた）。
    """
    from src.lock_manager import should_warn_recent_meal

    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=89))
    warning = should_warn_recent_meal(GRANDMA_ID)

    assert warning is not None, "89分前の食事は『直近』でなければならない"
    assert warning["minutes_ago"] == 89
    assert warning["last_meal_label"] == "昼食"
    assert ":" in warning["last_meal_time"], "家族へのLINE本文に出る時刻表記"


def test_91分前の食事は直近ではない(seeded_db):
    """窓を広げると、昼食のあと夕食前に炊飯器がロックされる。

    RECENT_MEAL_MINUTES=90 は「さっき食べたばかりなのにまた炊こうとしている」
    を捕まえるための窓。広げると正常な次の食事を妨害する。
    """
    from src.lock_manager import should_warn_recent_meal

    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=91))
    assert should_warn_recent_meal(GRANDMA_ID) is None


def test_ちょうど90分前は直近に含めない(seeded_db):
    """境界。実装は `minutes_ago < 90` の排他なので、90分台は対象外。

    insert してから判定するまでに必ず数ミリ秒進むので、90分ちょうどに
    置いた食事は 90.0x 分前として評価される。
    """
    from src.lock_manager import RECENT_MEAL_MINUTES, should_warn_recent_meal

    assert RECENT_MEAL_MINUTES == 90, "窓の値を変えるなら上下の境界テストも直すこと"
    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=90))
    assert should_warn_recent_meal(GRANDMA_ID) is None


def test_警告にはsession_idが入っている(seeded_db):
    """2026-10-15 追加。これが無いとロックの重複防止が成立しない。

    monitor 側は `warning["session_id"]` を重複防止集合のキーに使う。
    キーが消えると KeyError になり、その例外は session_aggregator の
    `except Exception` に飲まれて「静かにロックされないだけ」になる
    （＝また2ヶ月気づけないパターン）。
    """
    from src.lock_manager import should_warn_recent_meal

    sid = _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=10))
    warning = should_warn_recent_meal(GRANDMA_ID)

    assert warning is not None
    assert "session_id" in warning, "重複防止のキーが消えている"
    assert warning["session_id"] == sid, "どの食事セッションの話かが一致していること"


def test_monitorが読むキーは全部返り値に入っている(seeded_db):
    """lock_manager と monitor のキー契約を機械的に突き合わせる。

    monitor.session_aggregator が `warning[...]` で読むキーを AST で抜き出し、
    実際の返り値に全部あるかを見る。片方だけ直したときに落ちる。
    """
    from src.lock_manager import should_warn_recent_meal

    fn = _parse_func("src/monitor.py", "session_aggregator")
    read_keys = {
        n.slice.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Subscript)
        and isinstance(n.value, ast.Name) and n.value.id == "warning"
        and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)
    }
    assert read_keys, "monitor 側が warning を読んでいない（Layer 2 が消えた？）"

    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=10))
    warning = should_warn_recent_meal(GRANDMA_ID)
    assert warning is not None
    missing = read_keys - set(warning)
    assert not missing, f"monitor が読むのに返されていないキー: {missing}"


def test_お風呂セッションを直近の食事と数えない(seeded_db):
    """入浴直後に炊飯器がロックされかけた事故の回帰テスト。

    お風呂は meal_sessions に入るが食事ではない。ラベルで弾かないと
    「お風呂から出たら炊飯器が使えない」になる。
    """
    from src.lock_manager import should_warn_recent_meal

    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=5), label="お風呂")
    assert should_warn_recent_meal(GRANDMA_ID) is None


@pytest.mark.parametrize("label", ["起床", "お薬", "就寝", "髪洗った"])
def test_家族の手動スタンプを直近の食事と数えない(seeded_db, label):
    """家族が起床・お薬・就寝のスタンプを押した直後にロック確認が飛んでいた。

    monitor 側の食事カウントだけ食事ラベル限定にしても、実際にロックを
    発火させるこちら側が無条件では筋が通らない（sessions.last_meal_session
    のコメントに書かれている通り）。
    """
    from src.lock_manager import should_warn_recent_meal

    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=5), label=label)
    assert should_warn_recent_meal(GRANDMA_ID) is None


def test_お風呂の裏に古い食事があっても直近扱いにしない(seeded_db):
    """「最後のセッション」ではなく「最後の食事セッション」を見ているかの確認。

    5分前にお風呂、200分前に昼食。ラベルで絞ったあと時刻も見ていなければ
    どちらかで誤判定する。
    """
    from src.lock_manager import should_warn_recent_meal

    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=200), label="昼食")
    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=5), label="お風呂")
    assert should_warn_recent_meal(GRANDMA_ID) is None


def test_未確定の食事ではロック確認をしない(seeded_db):
    """Layer 2 は家族の確認後に動く（誤検知でロックすると祖母が困る）。

    confirmed=0 は LINE 確認待ち。これを拾うと、炊飯器の保温を食事と
    誤検知した段階で勝手にロックしてしまう。
    """
    from src.lock_manager import should_warn_recent_meal

    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=10), confirmed=0)
    assert should_warn_recent_meal(GRANDMA_ID) is None


def test_誤検知として却下された食事は数えない(seeded_db):
    """confirmed=-1 は家族が「食べていない」と答えたセッション。

    却下したのにロックされたら、家族は次から答えてくれなくなる。
    """
    from src.lock_manager import should_warn_recent_meal

    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=10), confirmed=-1)
    assert should_warn_recent_meal(GRANDMA_ID) is None


def test_他の家族の食事で祖母のロック確認をしない(seeded_db):
    """person_id を無視すると、家族が来て食べただけで炊飯器がロックされる。"""
    from src.lock_manager import should_warn_recent_meal

    _insert_meal_session(
        seeded_db, datetime.now() - timedelta(minutes=10), person_id=OTHER_FAMILY_ID
    )
    assert should_warn_recent_meal(GRANDMA_ID) is None


def test_食事の記録が無ければNone(seeded_db):
    from src.lock_manager import should_warn_recent_meal

    assert should_warn_recent_meal(GRANDMA_ID) is None


def test_確定が遅れた食事は確定直後でも直近ではない(seeded_db):
    """2ヶ月ロックが発火しなかった原因そのもの。

    家族の確認は食事開始から中央値7.6時間後（143件中84件が90分超）に来る。
    判定の基準は started_at なので、確定した瞬間にはもう窓が閉じている。
    つまり「カウントが増えたティックで判定する」設計では構造的に発火しない。
    このテストが通る＝毎ティック評価が必要であることの根拠が残る。
    """
    from src.lock_manager import should_warn_recent_meal

    started = datetime.now() - timedelta(hours=8)
    _insert_meal_session(seeded_db, started, confirmed_at=datetime.now(timezone.utc))
    assert should_warn_recent_meal(GRANDMA_ID) is None, (
        "8時間前に始まった食事は、いま確定しても『直近90分』ではない"
    )


# ======================================================================
# 2. 炊飯器ロックの発火条件（monitor.py Layer 2）
# ======================================================================

def _layer2_tick(asked: set[int]) -> dict | None:
    """monitor.session_aggregator の Layer 2 判定を1ティックぶん写したもの。

    本体は 60 秒ループの途中に素で書かれていて関数として呼べない。
    そこで「判定の形が崩れていないこと」は下の AST テストで縛り、
    ここでは重複防止の振る舞いを見る。
    """
    from src.lock_manager import should_warn_recent_meal

    warning = should_warn_recent_meal(GRANDMA_ID)
    if warning and warning["session_id"] not in asked:
        asked.add(warning["session_id"])
        return warning
    return None


def test_同じ食事セッションには一度しか聞かない(seeded_db):
    """重複防止が無いと家族のLINEに毎分ロック確認が飛ぶ。

    `_request_lock_confirmation` の通知キーは `%Y-%m-%d_%H%M_lockreq`（分単位）
    なので、pending_notifications 側の一意制約では重複を止められない。
    止めているのはプロセス内の `_lock_asked_sessions` だけ。
    """
    _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=10))
    asked: set[int] = set()

    fired = [_layer2_tick(asked) for _ in range(10)]
    assert sum(1 for f in fired if f) == 1, "10ティックで1回だけ聞くこと"
    assert fired[0] is not None, "最初のティックで聞くこと"


def test_次の食事では改めて聞く(seeded_db):
    """重複防止のキーが食事セッション単位であることの確認。

    もしキーが人物IDや固定値だったら、一度聞いた後は永久に聞かなくなる
    （＝また「2ヶ月発火しない」状態になる）。
    """
    first = _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=80))
    asked: set[int] = set()

    assert _layer2_tick(asked) is not None
    assert _layer2_tick(asked) is None

    second = _insert_meal_session(seeded_db, datetime.now() - timedelta(minutes=2))
    assert second != first
    again = _layer2_tick(asked)
    assert again is not None and again["session_id"] == second
    assert _layer2_tick(asked) is None, "2件目も1回だけ"


def test_ロック判定は食事カウントが増えたティックの中に無い(seeded_db):
    """**2026-08-09 から2ヶ月間ロックが死んでいた原因を構造で縛る。**

    以前は Layer 2 が `if prev_session_count is not None and
    current_count > prev_session_count:` の内側にあった。カウントが増えるのは
    セッション確定の瞬間で、そこから見ると食事は中央値7.6時間前。
    RECENT_MEAL_MINUTES=90 の窓は必ず閉じていて、30日間で送信0件だった。
    判定をこのブロックに戻したらこのテストが落ちる。
    """
    fn = _parse_func("src/monitor.py", "session_aggregator")
    calls = _find_calls(fn, "should_warn_recent_meal")
    assert len(calls) == 1, "Layer 2 の呼び出しが1箇所であることが前提"

    guards = _enclosing_if_tests(fn, calls[0])
    bad = [g for g in guards if "prev_session_count" in g]
    assert not bad, (
        "ロック判定が『カウントが増えたティック』の中に戻っている: "
        + " / ".join(bad)
        + "\nこの位置では確定が遅れた食事を拾えず、ロックは永久に発火しない。"
    )


def test_ロック判定には重複防止の集合がある(seeded_db):
    """毎ティック評価にした代償として、重複防止が必須になった。

    `_request_lock_confirmation` の ctx は分単位なので、集合を外すと
    毎分ロック確認のLINEが家族全員に飛ぶ（オオカミ少年化して、
    本当に必要な通知も読まれなくなる）。
    """
    fn = _parse_func("src/monitor.py", "session_aggregator")

    has_membership_guard = any(
        isinstance(n, ast.Compare)
        and any(isinstance(op, ast.NotIn) for op in n.ops)
        and "_lock_asked_sessions" in ast.unparse(n)
        and "session_id" in ast.unparse(n)
        for n in ast.walk(fn)
    )
    has_record = any(
        ast.unparse(n).startswith("_lock_asked_sessions.add")
        for n in _find_calls(fn, "add")
    )
    assert has_membership_guard, "session_id を使った重複チェックが無い"
    assert has_record, "聞いたセッションを記録していない（毎分再送になる）"


# ======================================================================
# 3. release_expired_locks() — 期限切れロックの解放
# ======================================================================

class _FakeMatter:
    """Matter WebSocket の差し替え。本番の matter-server は実機で動いている。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[int, bool]] = []

    async def __call__(self, node_id: int, on: bool) -> bool:
        self.calls.append((node_id, on))
        return self.ok


@pytest.fixture
def matter(monkeypatch) -> _FakeMatter:
    import src.lock_manager as lm

    fake = _FakeMatter()
    monkeypatch.setattr(lm, "_matter_set_power", fake)
    return fake


def test_ロックは期限と回数をDBに書く(temp_db, matter):
    """期限を DB に持たせることが永久ロック対策の本体。

    自動解除を in-process の sleep に持たせていた頃は、iot-monitor の
    再起動や停電で解除予約が消えて 50 時間ロックになった（2026-08-30）。
    unlock_due_at がここで入らないと cron は回収できない。
    """
    from src.lock_manager import LOCK_DURATION_MINUTES, lock_device

    assert asyncio.run(lock_device("rice_cooker", 1, reason="テスト")) is True
    assert matter.calls == [(1, False)], "ロックは通電OFF"

    row = _device_row(temp_db)
    assert row["is_locked"] == 1
    assert row["cycle_count_today"] == 1
    assert row["unlock_due_at"] is not None, "期限が無いと永久ロックになる"
    due = datetime.fromisoformat(row["unlock_due_at"])
    expected = datetime.now() + timedelta(minutes=LOCK_DURATION_MINUTES)
    assert abs((due - expected).total_seconds()) < 120


def test_Matterが失敗したらロック済みにしない(temp_db, matter):
    """**ここが逆になると「ロック済み」の嘘が家族UIに出る。**

    実際には通電しているのに家族ダッシュボードが🔒を出したら、家族は
    「炊飯器は止まっている」と信じて様子を見に行かなくなる。
    通信が失敗したら DB も変えないのが正しい。
    """
    from src.lock_manager import lock_device

    matter.ok = False
    assert asyncio.run(lock_device("rice_cooker", 1)) is False

    row = _device_row(temp_db)
    assert row["is_locked"] == 0, "通信失敗なのにロック済みと記録している"
    assert row["unlock_due_at"] is None
    assert row["cycle_count_today"] == 0


def test_手動解除で期限が消える(temp_db, matter):
    """期限が残ると、解除後にもう一度 cron の解放対象として拾われる。"""
    from src.lock_manager import unlock_device

    _insert_device_state(temp_db, unlock_due_at=_sql_now(+30), last_cycle_at=_sql_now(-90))
    assert asyncio.run(unlock_device("rice_cooker", 1, reason="家族による手動解除")) is True
    assert matter.calls == [(1, True)]

    row = _device_row(temp_db)
    assert row["is_locked"] == 0
    assert row["unlock_due_at"] is None


def test_解除が失敗したらロック状態を維持する(temp_db, matter):
    """解除できていないのに解除済みにすると、祖母は炊飯器が使えないままになる。"""
    from src.lock_manager import unlock_device

    matter.ok = False
    _insert_device_state(temp_db, unlock_due_at=_sql_now(+30))
    assert asyncio.run(unlock_device("rice_cooker", 1)) is False
    assert _device_row(temp_db)["is_locked"] == 1


def test_期限を過ぎたロックは解放される(temp_db, matter):
    """cron から回収する唯一の経路。ここが動かないと祖母は炊飯器を使えない。"""
    from src.lock_manager import release_expired_locks

    _insert_device_state(
        temp_db, unlock_due_at=_sql_now(-1), last_cycle_at=_sql_now(-121)
    )
    released = asyncio.run(release_expired_locks())

    assert released == ["rice_cooker"]
    assert matter.calls == [(1, True)], "Matter に通電ONを送ること"
    row = _device_row(temp_db)
    assert row["is_locked"] == 0
    assert row["unlock_due_at"] is None, "解放後に期限が残ると次回また解放処理に乗る"


def test_期限前のロックは触らない(temp_db, matter):
    """早すぎる解放は「食べたばかりなのに炊ける」になり、ロックの意味が消える。"""
    from src.lock_manager import release_expired_locks

    _insert_device_state(
        temp_db, unlock_due_at=_sql_now(+30), last_cycle_at=_sql_now(-90)
    )
    assert asyncio.run(release_expired_locks()) == []
    assert matter.calls == []
    assert _device_row(temp_db)["is_locked"] == 1


def test_期限がNULLのロックも自己修復して回収される(temp_db, matter):
    """2026-08-30 の50時間ロックそのもの。

    期限なしでロックされた行（この機能より前のロック、あるいは何かの経路で
    期限が落ちた行）は、放置すると家族が手動解除するまで永久にロックされる。
    last_cycle_at を起点に期限を補完して同じ実行の中で回収すること。
    """
    from src.lock_manager import release_expired_locks

    _insert_device_state(temp_db, unlock_due_at=None, last_cycle_at=_sql_now(-50 * 60))
    assert asyncio.run(release_expired_locks()) == ["rice_cooker"]
    assert _device_row(temp_db)["is_locked"] == 0


def test_期限の自己修復はlast_cycle_atの120分後になる(temp_db, matter):
    """補完した期限が妥当であること＋マイクロ秒付きの時刻を食えること。

    本番の last_cycle_at は `datetime.now()` のアダプタ経由なので
    "2026-10-15 17:45:00.123456" の形で入る。SQLite の datetime() が
    この形を扱えなければ結果は NULL になり、行は NULL のまま残って
    永久ロックに戻る（自己修復が静かに無効化される）。
    """
    from src.lock_manager import LOCK_DURATION_MINUTES, release_expired_locks

    assert LOCK_DURATION_MINUTES == 120
    _insert_device_state(temp_db, unlock_due_at=None, last_cycle_at=_sql_now(-30))

    assert asyncio.run(release_expired_locks()) == [], "30分前のロックはまだ期限内"
    row = _device_row(temp_db)
    assert row["unlock_due_at"] is not None, "期限の補完に失敗している（永久ロック）"
    assert row["is_locked"] == 1
    due = datetime.fromisoformat(row["unlock_due_at"])
    expected = datetime.now() + timedelta(minutes=LOCK_DURATION_MINUTES - 30)
    assert abs((due - expected).total_seconds()) < 120


def test_解除に失敗したロックは解放済みにしない(temp_db, matter):
    """Matter が落ちているときに解放済みと記録すると、ロックが残ったまま忘れられる。

    次回の cron で再試行できるよう、失敗時は DB を変えない。
    """
    from src.lock_manager import release_expired_locks

    matter.ok = False
    _insert_device_state(temp_db, unlock_due_at=_sql_now(-1), last_cycle_at=_sql_now(-121))

    assert asyncio.run(release_expired_locks()) == []
    row = _device_row(temp_db)
    assert row["is_locked"] == 1
    assert row["unlock_due_at"] is not None, "期限を消すと次回の回収対象から外れる"


def test_ロックされていない機器には何もしない(temp_db, matter):
    from src.lock_manager import release_expired_locks

    _insert_device_state(temp_db, is_locked=0, unlock_due_at=_sql_now(-60))
    assert asyncio.run(release_expired_locks()) == []
    assert matter.calls == []


def test_node_idが未登録の機器は自動解除されない(temp_db, matter):
    """**現在の仕様をそのまま固定している（望ましくない挙動）。**

    `/api/lock` は body の `device` をそのまま受けるので、家族UIから
    "rice_cooker" 以外の名前でロックできる。一方 DEVICE_NODE_IDS は
    rice_cooker だけなので、他の名前でロックされた行は自動解除の対象外になり、
    手動解除まで残る（＝50時間ロックの再現経路）。
    例外で cron 全体を落とさないことだけは保証されている。
    DEVICE_NODE_IDS に機器を足したら、このテストの期待値も一緒に直すこと。
    """
    from src.lock_manager import DEVICE_NODE_IDS, release_expired_locks

    assert "ih" not in DEVICE_NODE_IDS
    _insert_device_state(temp_db, device_name="ih",
                         unlock_due_at=_sql_now(-60), last_cycle_at=_sql_now(-180))

    assert asyncio.run(release_expired_locks()) == []
    assert matter.calls == [], "node_id が分からないので Matter は叩けない"
    assert _device_row(temp_db, "ih")["is_locked"] == 1, "ロックが残り続ける"


def test_解除できない機器があっても他の機器は解放する(temp_db, matter):
    """1台の失敗で残りを巻き込まないこと（cron は1日何度も回る）。"""
    from src.lock_manager import release_expired_locks

    _insert_device_state(temp_db, device_name="ih", unlock_due_at=_sql_now(-60))
    _insert_device_state(temp_db, device_name="rice_cooker", unlock_due_at=_sql_now(-60))

    assert asyncio.run(release_expired_locks()) == ["rice_cooker"]


# ======================================================================
# 4. garden.py — 祖母が毎日見る花
# ======================================================================

def test_達成数は花の段階に順番どおり対応する():
    from src.garden import FLOWER_TYPES, _done_to_flower

    for i, expected in enumerate(FLOWER_TYPES):
        assert _done_to_flower(i) is expected, f"達成{i}件の段階がずれている"
    assert _done_to_flower(0)["key"] == "seed"
    assert _done_to_flower(len(FLOWER_TYPES) - 1)["key"] == "bouquet"


@pytest.mark.parametrize("done", [8, 9, 20, 1000])
def test_段数を超える達成数でも満開で止まる(done):
    """上限の clamp が落ちると、全部達成した日にスコアが保存されなくなる。

    タブレットのスタンプは8個（起床/お薬/朝食/昼食/お風呂/髪洗った/夕食/就寝）で、
    FLOWER_TYPES も8段。全部達成すると done_count=8 になり、
    `min(done_count, len-1)` が無いと IndexError で save_daily_score が落ちる。
    /tablet は例外を飲んで画面だけ出すので、祖母の花は前日の値のまま止まり、
    誰も気づかない。
    """
    from src.garden import FLOWER_TYPES, _done_to_flower

    assert _done_to_flower(done) is FLOWER_TYPES[-1]


def test_タブレットのスタンプを全部達成しても例外にならない():
    """スタンプの数を増やしたときに clamp が足りているかを自動で見る。"""
    from src.garden import FLOWER_TYPES, _done_to_flower

    fn = _parse_func("src/web/app.py", "_build_stamps")
    stamp_lists = [
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.List)
        and any(getattr(t, "id", "") == "all_stamps" for t in n.targets)
    ]
    assert stamp_lists, "_build_stamps の all_stamps が見つからない"
    stamp_count = len(stamp_lists[0].elts)
    assert stamp_count >= 1

    flower = _done_to_flower(stamp_count)
    assert flower in FLOWER_TYPES
    if stamp_count >= len(FLOWER_TYPES):
        assert flower is FLOWER_TYPES[-1]


def test_達成数が負のときは下限clampが無く花が壊れる():
    """**現状の挙動をそのまま固定している（望ましくない挙動）。**

    `idx = min(done_count, len-1)` は上限だけを抑えていて下限が無い。実測:
      done_count = -1 → FLOWER_TYPES[-1] = 満開（何もしていない日に満開の花）
      done_count = -8 → 偶然 種
      done_count <= -9 → IndexError
    いまの呼び出し元（web/app.py と scripts/scheduled_notify.py）はどちらも
    len() で数えているので負値は到達しない。だから red にはしていないが、
    引き算で数えるコードが1つ増えた瞬間に、祖母の画面に嘘の花が出るか
    スコア保存が落ちる。`max(0, ...)` を入れて直すなら、この期待値も直すこと。
    """
    from src.garden import FLOWER_TYPES, _done_to_flower

    assert _done_to_flower(-1) is FLOWER_TYPES[-1], "下限 clamp が入った？ なら期待値を直す"
    assert _done_to_flower(-len(FLOWER_TYPES))["key"] == "seed"
    with pytest.raises(IndexError):
        _done_to_flower(-len(FLOWER_TYPES) - 1)


def test_花の色は日付で決まり必ず定義済みの色になる():
    """祖母の画面は例外で落ちてはいけない。色の添字が範囲外になると500になる。"""
    from src.garden import FLOWER_COLORS, _date_to_color

    d = date(2026, 1, 1)
    seen = set()
    for _ in range(800):
        color = _date_to_color(d)
        assert color in FLOWER_COLORS
        assert set(color) == {"name", "primary", "secondary", "center"}
        seen.add(color["name"])
        d += timedelta(days=1)
    assert len(seen) > 1, "全日同じ色なら日替わりの意味が無い"


def test_同じ日付なら必ず同じ色になる():
    """日付が変わらないのに色が変わると、祖母が同じ日の庭を見るたび別物になる。"""
    from src.garden import _date_to_color

    for d in (date(2026, 10, 15), date(2026, 2, 28), date(2027, 1, 1)):
        assert _date_to_color(d) is _date_to_color(d)


@pytest.mark.parametrize("d", [date(1, 1, 1), date(9999, 12, 31), date(2026, 12, 31)])
def test_極端な日付でも色の計算が落ちない(d):
    from src.garden import FLOWER_COLORS, _date_to_color

    assert _date_to_color(d) in FLOWER_COLORS


def test_同じ日のスコアは上書きされる(seeded_db):
    """/tablet は描画ごとに保存するので、UPSERT でないと1日に何百行も増える。"""
    from src.garden import save_daily_score

    today = date.today()
    save_daily_score(GRANDMA_ID, today, done_count=2, total_count=8)
    save_daily_score(GRANDMA_ID, today, done_count=5, total_count=8)

    conn = sqlite3.connect(seeded_db)
    try:
        rows = conn.execute(
            "SELECT done_count, flower_type FROM daily_scores WHERE person_id=? AND date=?",
            (GRANDMA_ID, today.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, "同じ日に複数行できている"
    assert rows[0][0] == 5
    assert rows[0][1] == "pretty", "達成数に対応する花が保存されていること"


def test_庭はデータが無い日を種で埋める(seeded_db):
    """抜けている日を飛ばすと、庭の並びが日付とずれて祖母が混乱する。"""
    from src.garden import get_garden_data, save_daily_score

    today = date.today()
    save_daily_score(GRANDMA_ID, today, done_count=3, total_count=8)

    garden = get_garden_data(GRANDMA_ID, days=5)
    assert len(garden) == 5
    assert [g["date"] for g in garden] == sorted(g["date"] for g in garden)
    assert garden[-1]["is_today"] is True
    assert garden[-1]["flower_type"] == "bud"
    assert all(g["flower_type"] == "seed" for g in garden[:-1])
    assert all(g["emoji"] and g["color_primary"] for g in garden), "描画に必要な項目"
    assert sum(1 for g in garden if g["is_today"]) == 1


def test_未知のflower_typeが入っていても庭が描ける(seeded_db):
    """DB に FLOWER_TYPES に無いキーが残っていても画面を落とさないこと。

    db.py のスキーマコメントは flower_type の値を
    seed/sprout/bud/flower/bloom/big/bouquet と書いているが、
    実際の FLOWER_TYPES は stem/pretty を持ち bloom を持たない。
    古い行や手で入れた行が 'bloom' のままでも祖母の画面は出す。
    """
    from src.garden import FLOWER_TYPES, get_garden_data

    today = date.today()
    conn = sqlite3.connect(seeded_db)
    try:
        conn.execute(
            """INSERT INTO daily_scores(person_id, date, done_count, total_count, flower_type)
               VALUES(?, ?, ?, ?, ?)""",
            (GRANDMA_ID, today.isoformat(), 4, 8, "bloom"),
        )
        conn.commit()
    finally:
        conn.close()

    garden = get_garden_data(GRANDMA_ID, days=2)
    assert garden[-1]["emoji"] == FLOWER_TYPES[0]["emoji"], "不明なキーは種にフォールバック"
    assert garden[-1]["flower_label"] == FLOWER_TYPES[0]["label"]


# ======================================================================
# 5. bath_monitor.py — お風呂の状態機械
# ======================================================================
# pytest-asyncio は未導入なので asyncio.run() で回す。
# 5分/30分を実時間で待つわけにはいかないので、bath_monitor が時間を知る
# 2つの経路（asyncio.sleep と datetime.now）だけを仮想時計に差し替える。

_REAL_SLEEP = asyncio.sleep


class _ClockExhausted(RuntimeError):
    """仮想時計の予算切れ。無限ループでテストが固まるのを防ぐ安全弁。"""


class _FakeClock:
    def __init__(self, start: datetime, budget: int = 2000):
        self.now = start
        self.budget = budget
        self.sleeps = 0

    async def sleep(self, seconds=0, *args, **kwargs):
        self.sleeps += 1
        if self.sleeps > self.budget:
            raise _ClockExhausted(f"asyncio.sleep が {self.budget} 回を超えた")
        self.now = self.now + timedelta(seconds=seconds or 0)
        await _REAL_SLEEP(0)   # 監視タスク以外にも実行権を渡す

    def install(self, monkeypatch) -> None:
        import src.bath_monitor as bm

        clock = self

        class _DatetimeShim:
            @staticmethod
            def now() -> datetime:
                return clock.now

        monkeypatch.setattr(bm.asyncio, "sleep", self.sleep)
        monkeypatch.setattr(bm, "datetime", _DatetimeShim)


class _Recorder:
    """BathMonitor のコールバック記録。"""

    def __init__(self):
        self.starts: list[datetime] = []
        self.ends: list[float] = []
        self.alerts: list[float] = []
        self.resolved: int = 0

    def kwargs(self, clock: _FakeClock) -> dict:
        async def on_start():
            self.starts.append(clock.now)

        async def on_end(duration_min: float):
            self.ends.append(duration_min)

        async def on_alert(elapsed: float):
            self.alerts.append(elapsed)

        async def on_resolved():
            self.resolved += 1

        return {
            "on_bath_start": on_start,
            "on_bath_end": on_end,
            "on_alert": on_alert,
            "on_alert_resolved": on_resolved,
        }


async def _pump_until(pred, limit: int = 400) -> bool:
    """監視タスクに実行権を渡し続け、条件が成立するか上限に達するまで回す。"""
    for _ in range(limit):
        if pred():
            return True
        await _REAL_SLEEP(0)
    return bool(pred())


async def _stop(monitor) -> None:
    task = monitor._monitor_task
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture
def bath(monkeypatch):
    """仮想時計を入れた BathMonitor の組み立て係。"""
    from src.bath_monitor import BathMonitor

    clock = _FakeClock(datetime(2026, 10, 15, 20, 0, 0))
    clock.install(monkeypatch)
    rec = _Recorder()

    def make(alert_minutes: int = 30):
        return BathMonitor(alert_minutes=alert_minutes, **rec.kwargs(clock))

    return make, rec, clock


def test_ドアが閉まっただけでは入浴開始にしない(bath):
    """「入浴後にドアを開けっ放し、家族が後で閉めた」を入浴にしないため。

    ここで即 on_bath_start を呼ぶと、祖母が入っていない入浴記録が残り、
    タブレットのスタンプに嘘が出る。
    """
    make, rec, clock = bath

    async def scenario():
        mon = make()
        await mon.door_closed()
        assert mon.is_in_bath is True
        assert rec.starts == [], "確定前に入浴開始を発火してはいけない"
        await _stop(mon)

    asyncio.run(scenario())


def test_5分以内のモーションで入浴が確定する(bath):
    """脱衣所のモーションが唯一の「本当に入った」証拠。"""
    make, rec, clock = bath

    async def scenario():
        mon = make()
        await mon.door_closed()
        await mon.motion_detected()

        assert rec.starts == [clock.now]
        await mon.motion_detected()
        assert len(rec.starts) == 1, "生存確認のモーションで入浴開始を再発火しない"
        await _stop(mon)

    asyncio.run(scenario())


def test_モーションが無ければ5分でキャンセルされる(bath):
    """誤検知の打ち切り。ここが無いと家族が夜中にドアを閉めるだけで監視が始まる。"""
    from src.bath_monitor import CONFIRM_MOTION_WINDOW_SECONDS

    make, rec, clock = bath
    assert CONFIRM_MOTION_WINDOW_SECONDS == 5 * 60

    async def scenario():
        mon = make()
        await mon.door_closed()
        assert await _pump_until(lambda: not mon.is_in_bath), "5分で打ち切られていない"

        assert rec.starts == []
        assert rec.alerts == []
        assert clock.now >= datetime(2026, 10, 15, 20, 5, 0), "5分ぶん待ってから切ること"
        await _stop(mon)

    asyncio.run(scenario())


def test_Phase1で打ち切られた後は監視が戻らない(bath):
    """**現在の挙動をそのまま仕様として固定している（危険な挙動）。**

    ドア閉から5分モーションが無いと監視が外れる。ところが物理的には
    ドアは閉まったままなので、次のドア閉イベントは来ない。つまり
    「ドアを閉めて静かに5分以上湯に浸かる」という、溺水の危険が最も高い
    入り方をしたときだけ、その入浴は最初から最後まで無監視になる。
    打ち切り後は bath_start も bath_end も 30分アラートも全部死ぬ。
    直すならこのテストも一緒に書き換えること。
    """
    make, rec, clock = bath

    async def scenario():
        mon = make()
        await mon.door_closed()
        assert await _pump_until(lambda: not mon.is_in_bath)
        task = mon._monitor_task
        assert task is not None and task.done(), "監視タスクが終了している"

        # 以降、祖母が動いても何も起きない
        await mon.motion_detected()
        assert rec.starts == [], "打ち切り後のモーションでは入浴確定しない"
        assert mon.is_in_bath is False

        # 1時間ぶん進めてもアラートは出ない（タイマーはもう無い）
        for _ in range(60):
            await clock.sleep(60)
        assert rec.alerts == []

        # ドアが開いても入浴終了は記録されない
        await mon.door_opened()
        assert rec.ends == []
        await _stop(mon)

    asyncio.run(scenario())


def test_確定後30分動きがなければアラートが1回出る(bath):
    """家族への緊急通知の本体。ここが出ないと倒れていても誰も気づかない。"""
    make, rec, clock = bath

    async def scenario():
        mon = make(alert_minutes=30)
        await mon.door_closed()
        await mon.motion_detected()

        assert await _pump_until(lambda: rec.alerts), "30分無動作でアラートが出ない"
        assert rec.alerts[0] >= 30
        assert clock.now >= datetime(2026, 10, 15, 20, 30, 0)

        # そのまま静かでも鳴らし続けない（再通知は notifier 側の責務）
        await _pump_until(lambda: len(rec.alerts) > 1, limit=120)
        assert len(rec.alerts) == 1, "同じ無動作で何度も鳴らさない"
        await _stop(mon)

    asyncio.run(scenario())


def test_アラート後にモーションが戻れば解決扱いになる(bath):
    """2026-09-25 の修正の回帰テスト。

    以前はモーションが戻っても pending が未応答のまま残り、
    recheck_pending が再通知を続けていた。実際に、動きが戻ったあとも
    通知が続き、誰も応答しないまま打ち切りに至った事例がある。
    """
    make, rec, clock = bath

    async def scenario():
        mon = make(alert_minutes=30)
        await mon.door_closed()
        await mon.motion_detected()
        assert await _pump_until(lambda: rec.alerts)

        await mon.motion_detected()
        assert rec.resolved == 1, "動きが戻ったらアラートの前提が消える"
        assert len(rec.starts) == 1, "解決は入浴開始の再発火ではない"

        # また静かになれば改めて鳴る
        assert await _pump_until(lambda: len(rec.alerts) > 1)
        assert rec.alerts[1] >= 30
        assert rec.resolved == 1
        await _stop(mon)

    asyncio.run(scenario())


def test_モーションが戻っただけでは解決通知を連発しない(bath):
    """アラートが出ていないのに「解決しました」が飛ぶと家族が混乱する。"""
    make, rec, clock = bath

    async def scenario():
        mon = make()
        await mon.door_closed()
        await mon.motion_detected()
        for _ in range(5):
            await mon.motion_detected()
        assert rec.resolved == 0
        await _stop(mon)

    asyncio.run(scenario())


def test_確定後にドアが開けば入浴終了になる(bath):
    make, rec, clock = bath

    async def scenario():
        mon = make()
        await mon.door_closed()
        await mon.motion_detected()
        await clock.sleep(12 * 60)   # 12分入浴
        await mon.door_opened()

        assert len(rec.ends) == 1
        assert 11.5 <= rec.ends[0] <= 13.0, f"入浴時間がおかしい: {rec.ends}"
        assert mon.is_in_bath is False
        assert mon.bath_duration_minutes is None
        await _stop(mon)

    asyncio.run(scenario())


def test_未確定のままドアが開いたら入浴として記録しない(bath):
    """ドア閉からモーション無しで開いた＝誰も入っていない。記録すると嘘になる。"""
    make, rec, clock = bath

    async def scenario():
        mon = make()
        await mon.door_closed()
        await clock.sleep(30)
        await mon.door_opened()

        assert rec.ends == []
        assert rec.starts == []
        assert mon.is_in_bath is False
        await _stop(mon)

    asyncio.run(scenario())


def test_入浴中の2回目のドア閉は無視される(bath):
    """接点センサーが連発しても入浴開始時刻と30分タイマーを巻き戻さないこと。

    巻き戻すと、動きが無いまま時間が過ぎてもアラートの起点が後ろにずれ続け、
    緊急通知が永遠に出ない。
    """
    make, rec, clock = bath

    async def scenario():
        mon = make()
        await mon.door_closed()
        await mon.motion_detected()
        first_task = mon._monitor_task
        started = mon._bath_start

        await clock.sleep(5 * 60)
        await mon.door_closed()

        assert mon._bath_start == started, "入浴開始時刻が巻き戻っている"
        assert mon._monitor_task is first_task, "監視タスクが二重に起動している"
        assert len(rec.starts) == 1
        await _stop(mon)

    asyncio.run(scenario())


def test_入浴していないときのイベントは無視される(bath):
    """センサーの単発ノイズで状態機械が壊れないこと。"""
    make, rec, clock = bath

    async def scenario():
        mon = make()
        await mon.motion_detected()
        await mon.door_opened()

        assert (rec.starts, rec.ends, rec.alerts, rec.resolved) == ([], [], [], 0)
        assert mon.is_in_bath is False
        assert mon.bath_duration_minutes is None

    asyncio.run(scenario())
