"""通知の構造的な穴を固定するテスト。

このシステムの最大の失敗モードは2つしかない。

1. **鳴るべき時に鳴らない** — ネット断で送信に失敗すると pending 行すら作られず、
   家族UIの未対応リストにも再通知にも一切乗らなかった（2026-05-21 の bath_emergency は
   LINE も pending も残らず、ログ1行だけになった）。
2. **解決済みの事象について鳴り続ける** — 前提が消えたことをシステム自身が
   観測しているのに pending を閉じないため、recheck_pending が再通知を続ける。
   2026-10-11 には前提が消えた18分後から更に3ラウンド・計15通が送られた。

どちらも実際に起きている。だからこのファイルは「通ったら安心」ではなく
「壊れたら気づける」ことだけを狙う。実装をなぞる確認は書かない。

**このファイルには現状落ちるテストが2件ある**（どちらも実装側の穴で、
テストの誤りではない）。xfail で隠さずに落としてある。
  - test_CRITICALカテゴリ全部に解決検知の経路がある
  - test_安否アラートは活動再開後に再通知されない

LINE API は一切叩かない（_no_line_api fixture が requests.post を落とす）。
"""
from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import event_bus, notifier
import scripts.recheck_pending as recheck

ROOT = Path(__file__).resolve().parent.parent

# 時刻を固定する基準。pending_notifications は UTC 保存なので UTC で持つ。
# 2026-10-15 12:00 UTC = JST 21:00（打ち切り通知の時刻表示テストで使う）。
FROZEN_UTC = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

# 架空の宛先。実在の LINE user ID・トークンは絶対に書かない。
FAKE_ENV = {
    "LINE_CHANNEL_ACCESS_TOKEN": "dummy-token-for-tests",
    "LINE_USER_ID": "Utest_admin_0000",
    "LINE_ALLOWED_SENDERS": "Utest_admin_0000,Utest_family_0001",
}


# ============================================================
# ローカルヘルパ（conftest は他エージェントと共用なので触らない）
# ============================================================

@pytest.fixture(autouse=True)
def _no_line_api(monkeypatch):
    """LINE API と本番 .env を完全に遮断する。

    notifier は `.env` から実トークンと実名（ADMIN_DISPLAY_NAME）を読む。
    テストが本物の Push を投げたら祖母宅の家族5人の端末に通知が飛ぶ。
    """
    def _boom(*args, **kwargs):
        raise AssertionError("テストが LINE API を実際に叩こうとした")

    monkeypatch.setattr(notifier.requests, "post", _boom)
    monkeypatch.setattr(notifier.requests, "put", _boom)
    monkeypatch.setattr(notifier, "_load_env", lambda: dict(FAKE_ENV))
    monkeypatch.setattr(notifier, "_admin_display_name", lambda: "テスト家族")


class _SendRecorder:
    """LINE 送信の代わりに本文を記録するだけのスタブ。

    returns は「何通送れたか」の偽装値。0 にするとネット断・宛先0件を再現できる。
    """

    def __init__(self, returns: int = 5) -> None:
        self.returns = returns
        self.plain: list[str] = []
        self.quick: list[tuple[str, list[dict]]] = []

    def broadcast_line_message(self, message: str) -> int:
        self.plain.append(message)
        return self.returns

    def broadcast_with_quick_reply(self, message: str, items: list[dict]) -> int:
        self.quick.append((message, items))
        return self.returns

    @property
    def all_messages(self) -> list[str]:
        return self.plain + [m for m, _ in self.quick]


@pytest.fixture
def recheck_sends(monkeypatch):
    """recheck_pending の時計を固定し、送信を記録用スタブに差し替える。

    `_utc_now` を固定しないと「15分ちょうど」の境界テストが秒の繰り上がりで
    フレークする。`_should_suppress_for_night` も実時刻依存なので、
    夜間テスト以外では無効化して「深夜に走らせたら結果が変わる」を防ぐ。
    """
    rec = _SendRecorder()
    monkeypatch.setattr(recheck, "_utc_now", lambda: FROZEN_UTC)
    monkeypatch.setattr(recheck, "_should_suppress_for_night", lambda category: False)
    monkeypatch.setattr(recheck, "broadcast_line_message", rec.broadcast_line_message)
    monkeypatch.setattr(recheck, "broadcast_with_quick_reply", rec.broadcast_with_quick_reply)
    return rec


def _utc_stamp(ago: timedelta) -> str:
    """FROZEN_UTC から ago だけ過去の、SQLite CURRENT_TIMESTAMP と同形式の文字列。"""
    return (FROZEN_UTC - ago).strftime("%Y-%m-%d %H:%M:%S")


def _insert_pending(db_path, notification_type: str, context_key: str, *,
                    message: str = "テスト通知\n2行目",
                    quick_items: list[dict] | None = None,
                    notify_count: int = 1,
                    created_ago: timedelta = timedelta(minutes=20),
                    last_notified_ago: timedelta | None = None,
                    completed: bool = False) -> int:
    """pending_notifications に架空の1行を入れる。時刻は UTC（本番と同じ）。"""
    if last_notified_ago is None:
        last_notified_ago = created_ago
    items = quick_items if quick_items is not None else [
        {"label": "✓ 了解（対応します）", "data": f"confirm:{notification_type}:{context_key}"},
    ]
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO pending_notifications
                   (notification_type, context_key, message, quick_reply_json,
                    created_at, last_notified_at, notify_count, completed_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (notification_type, context_key, message,
             json.dumps(items, ensure_ascii=False),
             _utc_stamp(created_ago), _utc_stamp(last_notified_ago),
             notify_count, _utc_stamp(timedelta(0)) if completed else None),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _pending(db_path, row_id: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM pending_notifications WHERE id = ?", (row_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _pending_by_key(db_path, notification_type: str, context_key: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT * FROM pending_notifications
                WHERE notification_type = ? AND context_key = ?""",
            (notification_type, context_key),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _freeze_notifier_clock(monkeypatch, when: datetime) -> None:
    """notifier 側の `datetime.now()` を固定する（夜間抑制の判定用）。

    notifier は `from datetime import datetime` なので、モジュール属性を
    差し替えれば `_is_night_quiet_hours` も recheck_pending が import 済みの
    `_should_suppress_for_night` も同じ固定時刻を見る。
    """
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(notifier, "datetime", _Frozen)


# --- ソースコードの静的監査用 ---

RESOLVER_FUNCS = {"mark_related_completed_silent", "mark_notification_completed"}


def _source_files() -> list[Path]:
    out: list[Path] = []
    for d in ("src", "scripts"):
        out.extend(sorted((ROOT / d).rglob("*.py")))
    return out


def _own_scope(fn: ast.AST):
    """その関数自身の本体だけを走査する（ネストした def の中身は含めない）。

    含めると monitor.py の巨大な main() が内側の _on_bath_end を飲み込み、
    無関係なカテゴリまで「解決経路あり」に見えてしまう。
    """
    stack = list(fn.body) + list(getattr(fn, "decorator_list", []))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _resolution_paths() -> dict[str, list[str]]:
    """「category 文字列と解決ヘルパの呼び出しが同じ関数にある」箇所を集める。

    解決経路の実装はどれも
      mark_related_completed_silent("<category>", <日付など>, "auto_resolved", ...)
    か、その category を同じ関数内のリテラルから渡す形になっている。
    asyncio.to_thread(fn, 引数...) 越しの呼び出しもあるので、
    引数位置ではなく「同じ関数スコープにあるか」で判定する。
    """
    found: dict[str, list[str]] = {}
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nodes = list(_own_scope(fn))
            used = {n.id for n in nodes if isinstance(n, ast.Name)}
            used |= {n.attr for n in nodes if isinstance(n, ast.Attribute)}
            if not (used & RESOLVER_FUNCS):
                continue
            for n in nodes:
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    found.setdefault(n.value, []).append(
                        f"{path.relative_to(ROOT)}:{fn.lineno} ({fn.name})"
                    )
    return {k: sorted(set(v)) for k, v in found.items()}


# ============================================================
# 1. 解決検知の経路（「解決済みなのに鳴り続ける」）
# ============================================================

def test_CRITICALカテゴリ全部に解決検知の経路がある():
    """**このテストは現状落ちる。実装の穴であってテストの誤りではない。**

    CRITICAL は未応答なら recheck_pending が最大3回・全家族に再通知する。
    つまり「前提が消えたことを観測したら pending を閉じる」経路が無いカテゴリは、
    解決済みの事象について鳴り続ける構造になっている。

    2026-10-11 の anomaly_inactivity がまさにそれで、前提が消えた18分後から
    更に3ラウンド・計15通（5人 × 3回）が送られた。
    bath_emergency だけは _on_bath_end / _on_bath_alert_resolved で閉じている。

    期待する直し方は「このテストに合わせて解決経路を足す」こと。
    テストを緩めると、この穴がまた2ヶ月見えなくなる。
    """
    paths = _resolution_paths()
    # 事後報告型は再通知自体をしないので、解決経路を持つ必要がない
    targets = notifier.CRITICAL_CATEGORIES - recheck.RETROSPECTIVE_CATEGORIES
    missing = sorted(c for c in targets if not paths.get(c))
    have = {c: paths[c] for c in sorted(targets) if paths.get(c)}
    assert not missing, (
        "解決を観測しても pending を閉じない CRITICAL カテゴリ: "
        + ", ".join(missing)
        + "\n（これらは解決後も最大3回・全家族に再通知される）"
        + "\n解決経路があるもの: "
        + json.dumps(have, ensure_ascii=False, indent=2)
    )


def test_安否アラートは活動再開後に再通知されない(seeded_db, recheck_sends):
    """**このテストは現状落ちる。実装の穴であってテストの誤りではない。**

    2026-10-11 の実例。日中センサー無反応で安否アラートを1通送った後、
    祖母が冷蔵庫を開けた＝アラートの前提は消えた。にもかかわらず誰も
    ボタンを押さなかったので、recheck_pending が再通知を続けた。

    anomaly_inactivity には「活動が再開したら pending を閉じる」経路が無く、
    events に活動再開が記録されていても escalate_unanswered は見ていない。
    """
    pid = _insert_pending(seeded_db, "anomaly_inactivity", "2026-10-15",
                          notify_count=1, created_ago=timedelta(minutes=20))
    # アラートの2分後に活動再開。events.started_at は JST なので JST で入れる。
    jst_now = (FROZEN_UTC + timedelta(hours=9)).replace(tzinfo=None)
    conn = sqlite3.connect(seeded_db)
    conn.execute(
        "INSERT INTO events(person_id, source, event_type, started_at) VALUES(1,'fridge','open',?)",
        ((jst_now - timedelta(minutes=18)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.commit()
    conn.close()

    recheck.escalate_unanswered()

    assert recheck_sends.all_messages == [], (
        "前提が消えた安否アラートが再通知された。"
        "活動再開を観測したら pending を閉じる経路が必要（notify_count="
        f"{_pending(seeded_db, pid)['notify_count']}）"
    )


def test_浴室緊急通知は解決を観測したら再通知が止まる(seeded_db, recheck_sends):
    """上2件の対照実験。解決経路があるカテゴリでは再通知が止まることを示す。

    これが通る＝上2件は「テストが厳しすぎる」のではなく実装側に穴がある。
    monitor._on_bath_end が実際に呼ぶのと同じ mark_related_completed_silent を使う。
    """
    today = "2026-10-15"
    pid = _insert_pending(seeded_db, "bath_emergency", f"{today}_2030",
                          notify_count=1, created_ago=timedelta(minutes=20))

    closed = notifier.mark_related_completed_silent(
        "bath_emergency", today, "auto_resolved", "入浴の終了を検知したため自動で解決",
    )
    assert closed == 1, "入浴終了で未応答の浴室緊急通知が閉じられなかった"

    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == [], "閉じたはずの浴室緊急通知が再通知された"
    assert _pending(seeded_db, pid)["completed_by"] == "auto_resolved"


def test_解決マークは対応済みの行を書き換えない(seeded_db):
    """家族が先に対応していたら、後から来た自動解決で上書きしない。

    上書きすると「誰が対応したか」が auto_resolved に化け、家族間で
    重複対応が起きる（誰が見たのか分からなくなる）。
    """
    today = "2026-10-15"
    pid = _insert_pending(seeded_db, "bath_emergency", f"{today}_2030",
                          notify_count=1, completed=True)
    conn = sqlite3.connect(seeded_db)
    conn.execute("UPDATE pending_notifications SET completed_by='Utest_family_0001' WHERE id=?", (pid,))
    conn.commit()
    conn.close()

    closed = notifier.mark_related_completed_silent(
        "bath_emergency", today, "auto_resolved", "自動で解決",
    )
    assert closed == 0
    assert _pending(seeded_db, pid)["completed_by"] == "Utest_family_0001"


def test_完了済みpendingは同じcontextの再発火で復活しない(seeded_db):
    """解決済みの事象が同じ context_key で再記録されても、再通知ループに戻さない。

    戻すと completed_at が消え、解決済みの件が再び最大3回鳴る。
    """
    pid = _insert_pending(seeded_db, "meal_alert", "2026-10-15_3",
                          notify_count=2, completed=True)
    returned = notifier.record_pending_notification(
        "meal_alert", "2026-10-15_3", "もう一度同じ通知", [], delivered=True,
    )
    row = _pending(seeded_db, pid)
    assert returned == pid
    assert row["completed_at"] is not None, "完了済みの pending が未完了に戻った"
    assert row["notify_count"] == 2, "完了済みなのに再通知回数が増えた"


# ============================================================
# 2. RETROSPECTIVE_CATEGORIES（事後報告型は再通知しない）
# ============================================================

def test_事後報告型は再通知されない(seeded_db, recheck_sends):
    """2026-09-26 10:46 に、既に解決した事象について20通(5人×4回)送った事故の回帰。

    long_toilet_stay は toilet_door の close ハンドラからしか発火せず、
    本人がドアを開けて出てきた後にしか鳴らない（実測53件中51件が close ±1秒）。
    事後報告を再通知しても家族にできることは何もない。
    """
    pid = _insert_pending(seeded_db, "long_toilet_stay", "2026-10-15_2030_long_toilet",
                          notify_count=1, created_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == [], "事後報告型（トイレ長時間滞在）が再通知された"
    assert _pending(seeded_db, pid)["notify_count"] == 1


def test_事後報告型は打ち切り通知も出ない(seeded_db, recheck_sends):
    """`continue` の位置が count 判定より後ろに移ると、事後報告型にも
    「⏰ 誰も応答しませんでした」が飛ぶ。本人は無事に出てきているので無意味な不安を与える。
    """
    _insert_pending(seeded_db, "long_toilet_stay", "2026-10-15_2030_long_toilet",
                    notify_count=recheck.MAX_NOTIFY_COUNT,
                    created_ago=timedelta(minutes=50),
                    last_notified_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == []


def test_事後報告型と同条件の緊急通知は再通知される(seeded_db, recheck_sends):
    """直前2件が「再通知の仕組み自体が死んでいる」ので通っているのではないことの確認。

    同じ経過時間・同じ notify_count で、bath_emergency なら1通飛ぶ。
    """
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    notify_count=1, created_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert len(recheck_sends.quick) == 1, "緊急通知の再通知そのものが動いていない"
    assert "まだ確認されていません（2/3回目）" in recheck_sends.quick[0][0]


def test_RETROSPECTIVE_CATEGORIESはCRITICALの部分集合():
    """非CRITICALを入れても `is_critical_category` の手前で弾かれるので死にコードになる。

    「再通知を止めたつもりが、実は別の理由で止まっていただけ」を防ぐ。
    """
    assert recheck.RETROSPECTIVE_CATEGORIES <= notifier.CRITICAL_CATEGORIES, (
        "RETROSPECTIVE に非CRITICAL が混ざっている: "
        f"{sorted(recheck.RETROSPECTIVE_CATEGORIES - notifier.CRITICAL_CATEGORIES)}"
    )


# ============================================================
# 3. 送信に失敗しても記録は残す（「鳴るべき時に鳴らない」）
# ============================================================

def test_送信0件でもpendingに記録される(seeded_db, monkeypatch):
    """ネット断で通知が丸ごと記録から消える経路があった。

    以前は `if sent > 0:` だったため、送信に失敗すると pending 行すら作られず、
    家族UIの未対応リストにも再通知にも一切乗らなかった。
    届かなかった通知こそ記録に残す必要がある。
    """
    monkeypatch.setattr(notifier, "broadcast_with_quick_reply", lambda msg, items: 0)
    sent = notifier.send_actionable_notification(
        "bath_emergency", "2026-10-15_2030", "🛁 テスト通知",
    )
    assert sent == 0
    row = _pending_by_key(seeded_db, "bath_emergency", "2026-10-15_2030")
    assert row, "送信0件のとき pending 行が作られなかった（通知が記録から消える）"


def test_未送信のpendingはnotify_count0で始まる(seeded_db):
    """1通も出せていないのに「1回通知済み」に見えると、再通知の枠を1つ無駄に消費する。

    家族が実際に受け取る回数が MAX_NOTIFY_COUNT より少なくなる。
    """
    pid = notifier.record_pending_notification(
        "bath_emergency", "2026-10-15_2030", "🛁 テスト通知", [], delivered=False,
    )
    assert _pending(seeded_db, pid)["notify_count"] == 0


def test_送信できたpendingはnotify_count1で始まる(seeded_db):
    pid = notifier.record_pending_notification(
        "bath_emergency", "2026-10-15_2031", "🛁 テスト通知", [], delivered=True,
    )
    assert _pending(seeded_db, pid)["notify_count"] == 1


def test_notify_count0は再通知の枠を消費しない(seeded_db, recheck_sends):
    """初回が1通も出せなかった件は、次の再通知が「1/3回目」として扱われる。

    ここが 2/3 から始まると、ネット断に当たった家族は本来3回のお知らせを
    2回しか受け取らない。
    """
    pid = _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                          notify_count=0, created_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert len(recheck_sends.quick) == 1
    assert "まだ確認されていません（1/3回目）" in recheck_sends.quick[0][0]
    assert _pending(seeded_db, pid)["notify_count"] == 1


def test_送信失敗でpendingを捨てる経路がmonitorに残っている():
    """`send_actionable_notification` では直したが、monitor.py には未修正が残っている。

    `if sent > 0:` の中でだけ record_pending_notification を呼ぶ形は、
    ネット断のときに通知を記録ごと失う（= 家族UIにも再通知にも乗らない）。
    2026-10-15 時点で5箇所。**増えたら落ちる。** 直したらこの数字を下げること。
    """
    monitor = ROOT / "src" / "monitor.py"
    tree = ast.parse(monitor.read_text(encoding="utf-8"))
    dropped: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or "sent" not in _names(node.test):
            continue
        if any("record_pending_notification" in _names(stmt) for stmt in node.body):
            dropped.append(node.lineno)
    dropped = sorted(set(dropped))
    assert len(dropped) <= 5, (
        f"送信失敗で pending を捨てる経路が増えた: src/monitor.py の {dropped} 行目"
    )


def test_ネット断でもQuickReply通知はoutboxに積まれる(tmp_path, monkeypatch):
    """最も命に関わる通知だけがネット断で確実に消えていた経路の回帰。

    以前はここが `except Exception: return False` だけで、平文の
    send_line_message しか outbox に守られていなかった。2026-05-21 の
    bath_emergency は LINE も pending も残らず、ログ1行だけになった。
    outbox に quickReply 込みの dict を積めば、再送時もボタンが生きる。
    """
    import requests as _requests
    monkeypatch.chdir(tmp_path)  # outbox は相対パス data/line_outbox.jsonl

    def _down(*args, **kwargs):
        raise _requests.exceptions.ConnectionError("ネット断のテスト")

    monkeypatch.setattr(notifier.requests, "post", _down)
    ok = notifier.send_line_with_quick_reply(
        "🛁 浴室の緊急通知", [{"label": "✓ 了解", "data": "confirm:bath_emergency:x"}],
        user_id="Utest_family_0001",
    )
    assert ok is False
    lines = (tmp_path / "data" / "line_outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "ネット断の Quick Reply 通知が outbox に積まれず消えた"
    entry = json.loads(lines[0])
    assert entry["to"] == "Utest_family_0001"
    assert entry["messages"][0]["quickReply"]["items"], "再送時にボタンが消える"


def test_ネット断でも平文通知はoutboxに積まれる(tmp_path, monkeypatch):
    import requests as _requests
    monkeypatch.chdir(tmp_path)

    def _down(*args, **kwargs):
        raise _requests.exceptions.ConnectionError("ネット断のテスト")

    monkeypatch.setattr(notifier.requests, "post", _down)
    assert notifier.send_line_message("テスト通知", user_id="Utest_family_0001") is False
    lines = (tmp_path / "data" / "line_outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ============================================================
# 4. エスカレーションの時間境界
# ============================================================

def test_最後の通知から15分ちょうどでは再通知しない(seeded_db, recheck_sends):
    """`COALESCE(last_notified_at, created_at) < cutoff` は厳密比較。

    しきい値ぴったりでは鳴らない。cron が `*/5` なので 15分という値は
    必ず tick に重なり、実測間隔が20分になる原因がここ。
    """
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    notify_count=1,
                    created_ago=timedelta(minutes=30),
                    last_notified_ago=timedelta(minutes=recheck.ESCALATE_AFTER_MINUTES))
    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == []


def test_15分を1秒超えたら再通知する(seeded_db, recheck_sends):
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    notify_count=1,
                    created_ago=timedelta(minutes=30),
                    last_notified_ago=timedelta(minutes=recheck.ESCALATE_AFTER_MINUTES, seconds=1))
    recheck.escalate_unanswered()
    assert len(recheck_sends.quick) == 1


def test_5分おきcronでは3回目と打ち切りが上限に収まる():
    """定数を触ったときに「打ち切り通知が永久に出なくなる」のを防ぐ。

    cron は `*/5`、しきい値は厳密比較なので実測間隔は
    ESCALATE_AFTER_MINUTES を5分単位で切り上げた値（15分→20分）になる。
    その結果 3回目は発生から40分、打ち切りは60分に落ちる。

    ここで ESCALATE_AFTER_MINUTES を30に伸ばすと3回目が70分になり、
    MAX_ESCALATE_AGE_MINUTES=60 に弾かれて notify_count が2で止まる。
    すると count==MAX_NOTIFY_COUNT に永久に到達せず、
    「誰も見ていなかった事実だけは必ず残す」という約束が黙って破れる。
    """
    CRON_INTERVAL = 5  # GW_SETUP.md: */5 * * * * scripts/recheck_pending.py
    schedule = [0.0]
    for _ in range(recheck.MAX_NOTIFY_COUNT):  # 2回目以降 + 打ち切り
        last = schedule[-1]
        tick = last
        while tick - last <= recheck.ESCALATE_AFTER_MINUTES:  # 厳密比較
            tick += CRON_INTERVAL
        schedule.append(tick)

    *renotifies, giveup = schedule[1:]
    assert renotifies[-1] <= recheck.MAX_ESCALATE_AGE_MINUTES, (
        f"最後の再通知が発生から {renotifies[-1]}分 で、"
        f"MAX_ESCALATE_AGE_MINUTES={recheck.MAX_ESCALATE_AGE_MINUTES} に弾かれる。"
        "notify_count が上限に届かず打ち切り通知が永久に出なくなる"
    )
    assert giveup <= recheck.MAX_GIVEUP_AGE_MINUTES, (
        f"打ち切りが発生から {giveup}分 で "
        f"MAX_GIVEUP_AGE_MINUTES={recheck.MAX_GIVEUP_AGE_MINUTES} を超える"
    )
    # 2026-10-15 時点の実測値。定数を変えたらここも更新すること（上2件が
    # 通っているなら安全側ではあるので、落ちたら値の確認だけで済む）。
    assert schedule[1:] == [20, 40, 60], f"実測スケジュールが変わった: {schedule}"


def test_上限回で打ち切り通知を出すがcompleted_atは埋めない(seeded_db, recheck_sends):
    """completed_at を埋めると /api/pending-notifications の
    `WHERE completed_at IS NULL` から外れ、家族ダッシュボードの
    「📨 未対応の確認」から約45分で消える。LINE再通知を廃止したときの
    代替がその一覧なので、LINE を諦めたからといって一覧から消すのは本末転倒。
    """
    pid = _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                          message="🛁 浴室に入ってから 30 分、反応がありません\n時刻: 20:30",
                          notify_count=recheck.MAX_NOTIFY_COUNT,
                          created_ago=timedelta(minutes=50),
                          last_notified_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()

    assert len(recheck_sends.plain) == 1, "打ち切り通知が送られなかった"
    assert "誰も応答しませんでした" in recheck_sends.plain[0]
    row = _pending(seeded_db, pid)
    assert row["completed_at"] is None, "打ち切りで家族UIの未対応リストから消える"
    assert row["notify_count"] == recheck.MAX_NOTIFY_COUNT + 1


def test_打ち切り済みは二度と鳴らない(seeded_db, recheck_sends):
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    notify_count=recheck.MAX_NOTIFY_COUNT + 1,
                    created_ago=timedelta(minutes=70),
                    last_notified_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == []


def test_発生から60分を超えた通知は再通知しない(seeded_db, recheck_sends):
    """cron停止・再起動で溜まった古い pending を掘り起こして
    「今ごろ届く昔の通知」を送らないための上限（2026-05-15 に再通知を全廃した理由）。
    """
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    notify_count=1,
                    created_ago=timedelta(minutes=recheck.MAX_ESCALATE_AGE_MINUTES + 10),
                    last_notified_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == []


def test_打ち切り通知だけは60分を超えても送る(seeded_db, recheck_sends):
    """cron の tick が1回ズレただけで打ち切りが恒久的に消えるのを防ぐための猶予。

    打ち切りは1通だけの事後報告なので多少遅れても混乱しない（本文に発生時刻が入る）。
    """
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    notify_count=recheck.MAX_NOTIFY_COUNT,
                    created_ago=timedelta(minutes=120),
                    last_notified_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert len(recheck_sends.plain) == 1


def test_発生から180分を超えたら打ち切り通知も送らない(seeded_db, recheck_sends):
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    notify_count=recheck.MAX_NOTIFY_COUNT,
                    created_ago=timedelta(minutes=recheck.MAX_GIVEUP_AGE_MINUTES + 20),
                    last_notified_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == []


def test_打ち切り通知が1通も出せなければ回数を進めない(seeded_db, recheck_sends):
    """送れていないのに「通知済み」にすると、誰も見ていなかった事実が消える。

    次の tick で再試行できるよう notify_count は据え置く。
    """
    recheck_sends.returns = 0
    pid = _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                          notify_count=recheck.MAX_NOTIFY_COUNT,
                          created_ago=timedelta(minutes=50),
                          last_notified_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert _pending(seeded_db, pid)["notify_count"] == recheck.MAX_NOTIFY_COUNT


def test_再通知が1通も出せなければ時刻を進めない(seeded_db, recheck_sends):
    """last_notified_at を進めると、届いていないのに次の15分を待つことになる。"""
    recheck_sends.returns = 0
    before_at = _utc_stamp(timedelta(minutes=20))
    pid = _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                          notify_count=1,
                          created_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    row = _pending(seeded_db, pid)
    assert row["notify_count"] == 1
    assert row["last_notified_at"] == before_at


def test_対応済みの通知は再通知されない(seeded_db, recheck_sends):
    """家族がボタンを押した後も鳴り続けたら、通知自体が信用されなくなる。"""
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    notify_count=1, created_ago=timedelta(minutes=20), completed=True)
    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == []


def test_非CRITICALは再通知されない(seeded_db, recheck_sends):
    """食事の人物確認のような雑務系を鳴らし直すと、緊急通知が埋もれる。"""
    assert "session_confirm" not in notifier.CRITICAL_CATEGORIES
    _insert_pending(seeded_db, "session_confirm", "session_42",
                    notify_count=1, created_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert recheck_sends.all_messages == []


# ============================================================
# 5. 時刻系の混在（UTC と JST）
# ============================================================

def test_pending_notificationsのcreated_atはUTC(seeded_db):
    """pending_notifications は CURRENT_TIMESTAMP = UTC で記録される。

    今日このセッションで2回、時刻系を読み違えて誤った結論を出している。
    どちらの列がどちらの時刻系かをテストで固定する。
    """
    pid = notifier.record_pending_notification(
        "bath_emergency", "2026-10-15_2030", "テスト通知", [], delivered=True,
    )
    stored = recheck._parse_utc(_pending(seeded_db, pid)["created_at"])
    drift = abs((stored - datetime.now(timezone.utc)).total_seconds())
    assert drift < 10, (
        f"created_at が UTC でない（UTC現在との差 {drift:.0f}秒）。"
        "ここが JST に変わると recheck_pending の 24時間タイムアウトと"
        "再通知の経過時間計算が9時間ずれる"
    )


def test_eventsのstarted_atはJST(seeded_db):
    """events.started_at は Python の datetime.now() = JST naive で保存される。

    SQLite の datetime('now') は UTC なので、直接比較するとタイムゾーン不一致で
    バグる（monitor._ask_rice_action_classification が Python 側で cutoff を
    計算しているのはこのため）。
    """
    event_id = asyncio.run(event_bus.record_event(
        source="fridge", event_type="open", person_id=1,
    ))
    conn = sqlite3.connect(seeded_db)
    try:
        raw = conn.execute(
            "SELECT started_at FROM events WHERE id = ?", (event_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    stored = datetime.fromisoformat(str(raw))
    assert stored.tzinfo is None, "started_at に tz が付くと既存の文字列比較が壊れる"
    assert abs((stored - datetime.now()).total_seconds()) < 10, (
        f"started_at がローカル時刻(JST)でない: {raw}"
    )
    utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((stored - utc_naive).total_seconds()) > 3600, (
        "started_at が UTC になっている。events(JST) と "
        "pending_notifications(UTC) の混在が崩れると、"
        "食事検知・炊飯器ロックの時間窓が9時間ずれる"
    )


def test_打ち切り通知の時刻表示はUTCに9時間足したJST(seeded_db, recheck_sends):
    """`jst = created + timedelta(hours=9)` が消えると、家族に9時間ずれた時刻が届く。

    created_at を JST 保存に「直した」場合もこのテストが落ちる。
    どちらの変更でも家族が見る時刻が狂うので、両方を1本で止める。
    """
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_2030",
                    message="🛁 浴室に入ってから 30 分、反応がありません",
                    notify_count=recheck.MAX_NOTIFY_COUNT,
                    created_ago=timedelta(minutes=50),   # 11:10 UTC = 20:10 JST
                    last_notified_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()
    assert len(recheck_sends.plain) == 1
    body = recheck_sends.plain[0]
    assert "20:10" in body, f"打ち切り通知の時刻が JST になっていない: {body!r}"
    assert "11:10" not in body, "UTC のまま家族に送っている"


def test_本番と同じAsia_Tokyoで動かす前提():
    """打ち切り通知の JST 変換は +9 固定なので、TZ がずれると表示が全部狂う。

    祖母宅の Raspberry Pi は Asia/Tokyo。TZ が UTC に戻ったら気づけるようにする。
    """
    offset = datetime.now().astimezone().utcoffset()
    assert offset == timedelta(hours=9), (
        f"ローカルTZが JST でない（offset={offset}）。"
        "events.started_at と打ち切り通知の +9h 変換が成り立たない"
    )


# ============================================================
# 6. 夜間の抑制
# ============================================================

@pytest.mark.parametrize("hour,expected", [
    (0, False),   # 0時台はまだ通す
    (1, True),    # 抑制開始
    (4, True),    # 抑制の最後の時間帯
    (5, False),   # 5時から解除
    (12, False),
    (23, False),
])
def test_夜間抑制の時間境界(monkeypatch, hour, expected):
    """1:00〜5:00 の外で抑制が効くと、昼の食事アラートが黙って消える。

    内側で効かないと、深夜に食事アラートで祖母と家族を起こす。
    """
    _freeze_notifier_clock(monkeypatch, datetime(2026, 10, 15, hour, 30))
    assert notifier._should_suppress_for_night("meal_alert") is expected


def test_深夜に通るのはNIGHT_ALLOWEDだけ(monkeypatch):
    """CRITICAL でも NIGHT_ALLOWED でなければ深夜は鳴らない。

    「CRITICAL だから深夜も鳴る」と誤解した変更を止める。
    """
    _freeze_notifier_clock(monkeypatch, datetime(2026, 10, 15, 3, 0))
    allowed = {c for c in notifier.CRITICAL_CATEGORIES
               if not notifier._should_suppress_for_night(c)}
    assert allowed == notifier.NIGHT_ALLOWED_CATEGORIES & notifier.CRITICAL_CATEGORIES
    # 深夜に鳴らしてはいけないもの（食事・ロックは朝でいい）
    assert notifier._should_suppress_for_night("meal_alert") is True
    assert notifier._should_suppress_for_night("device_locked") is True
    # 命に関わるものは必ず鳴る
    for category in ("bath_emergency", "anomaly_inactivity", "bath_abnormal_temp"):
        assert notifier._should_suppress_for_night(category) is False, category


def test_NIGHT_ALLOWEDはCRITICALの部分集合():
    """非CRITICAL を NIGHT_ALLOWED に入れると、深夜に admin 1人だけに鳴る。

    「深夜でも鳴らす」と書いたのに家族には届かない、という食い違いになる。
    """
    assert notifier.NIGHT_ALLOWED_CATEGORIES <= notifier.CRITICAL_CATEGORIES, (
        "深夜許可なのに全家族に飛ばないカテゴリ: "
        f"{sorted(notifier.NIGHT_ALLOWED_CATEGORIES - notifier.CRITICAL_CATEGORIES)}"
    )


def test_深夜に抑制された通知はpendingにも残らない(seeded_db, monkeypatch):
    """**現在の仕様**: 深夜に抑制されたカテゴリは記録ごと消える。

    再通知ループに乗せないための意図的な挙動だが、
    「鳴らない」だけでなく家族UIの未対応リストにも残らない。
    23時台に出た meal_alert が1時以降に再評価されることも無い。
    仕様を変えて pending を残すようにしたら、このテストで気づける。
    """
    _freeze_notifier_clock(monkeypatch, datetime(2026, 10, 15, 3, 0))
    called: list[str] = []
    monkeypatch.setattr(notifier, "broadcast_with_quick_reply",
                        lambda msg, items: called.append(msg) or 5)
    sent = notifier.send_actionable_notification("meal_alert", "2026-10-15_3", "🍚 テスト")
    assert sent == 0
    assert called == []
    assert _pending_by_key(seeded_db, "meal_alert", "2026-10-15_3") == {}


def test_深夜の再通知もNIGHT_ALLOWEDだけ(seeded_db, recheck_sends, monkeypatch):
    """23時台に作られた pending が深夜1時に再通知されて家族を起こすのを防ぐ。

    recheck_pending は notifier._should_suppress_for_night を通すので、
    ここでは本物の判定を使って配線ごと確認する。
    """
    monkeypatch.setattr(recheck, "_should_suppress_for_night",
                        notifier._should_suppress_for_night)
    _freeze_notifier_clock(monkeypatch, datetime(2026, 10, 15, 3, 0))

    _insert_pending(seeded_db, "meal_alert", "2026-10-15_3",
                    message="🍚 深夜に鳴ってはいけない食事アラート",
                    notify_count=1, created_ago=timedelta(minutes=20))
    _insert_pending(seeded_db, "bath_emergency", "2026-10-15_0240",
                    message="🛁 深夜でも必ず鳴る浴室アラート",
                    notify_count=1, created_ago=timedelta(minutes=20))
    recheck.escalate_unanswered()

    bodies = recheck_sends.all_messages
    assert len(bodies) == 1, f"深夜に鳴ってはいけない通知が飛んだ: {bodies}"
    assert "浴室アラート" in bodies[0], f"深夜に鳴るべき通知が入れ替わっている: {bodies[0]!r}"
    assert "食事アラート" not in bodies[0]
    assert _pending_by_key(seeded_db, "meal_alert", "2026-10-15_3")["notify_count"] == 1


# ============================================================
# 7. マスタースイッチ（家族が通知を止めているとき）
# ============================================================

def test_マスタースイッチOFFならLINEを叩かない(seeded_db):
    """祖母宅の運用で実際に ON/OFF している。OFF のとき1通でも飛んだら信用を失う。

    _no_line_api fixture が requests.post を落とすので、
    実際に送信しようとしたらここで AssertionError になる。
    """
    from src.settings import set_setting
    set_setting("notify_master_enabled", "0")
    assert notifier.send_line_message("テスト通知") is False
    assert notifier.broadcast_line_message("テスト通知") == 0
    assert notifier.send_line_with_quick_reply("テスト通知", []) is False


def test_マスタースイッチOFFでも記録は残る(seeded_db):
    """通知を止めている間の事象が記録から消えると、後から誰も気づけない。

    notify_count=0 なので、再通知の枠も消費しない。
    """
    from src.settings import set_setting
    set_setting("notify_master_enabled", "0")
    sent = notifier.send_actionable_notification(
        "bath_emergency", "2026-10-15_2030", "🛁 テスト通知",
    )
    assert sent == 0
    row = _pending_by_key(seeded_db, "bath_emergency", "2026-10-15_2030")
    assert row, "マスタースイッチOFFで事象が記録から消えた"
    assert row["notify_count"] == 0
