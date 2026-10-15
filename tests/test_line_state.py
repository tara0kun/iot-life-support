"""LINE の1タップ・1メッセージが祖母の記録と炊飯器に何を書くか（担当: 状態変化）。

このファイルが見ているのは「返信の文面」ではなく **DB と機器に何が起きたか** である。
LINE のボタンは家族のトーク履歴に永久に残り、押せば即座に本番 DB に書き込まれる。
しかも書き込みの大半は監査ログを残さない（家族UIの削除は `edit_log` に残るのに、
LINE 経由の削除は残らない）。壊れても誰も気づけないので、ここで固定する。

実測で分かった主要な事実（詳細は各テストの docstring）:

  - `rice_action:<event_id>:keep_warm` は **event_id の中身を一切確認せずに**
    `events` を1行消す。炊飯器と無関係なイベント（お風呂・トイレ）でも消える。
    さらに `meal_sessions` → `meal_photos` が CASCADE で消える。
  - `merge:<new>:<prev>` は人物も時刻も確認しない。母の夕食を祖母の昼食に
    吸収させて `events.person_id` ごと書き換えられる。
  - `attribute:` / `rice_action:...:cook` は「記録しました」と返すのに
    `meal_sessions.confirmed` を 1 にしないので、`sessions_today()` に出ない
    = 「状況」の食事回数に入らない。
  - `attribute:` / `sess_confirm:` には「他の家族が既に回答済み」のガードが無く、
    2人目の回答が1人目を黙って上書きする（しかもブロードキャストは出ない）。
  - Matter の OFF が成功した直後に DB 書き込みが失敗すると、炊飯器の電源だけが
    切れて DB は「解除済み」のまま残る。cron の期限解除も `is_locked=1` しか
    見ないので、どの経路からも復旧しない。

**本番に副作用を出さないための約束**
  - DB は `temp_db` / `seeded_db` fixture の一時ファイルのみ（conftest の
    `_guard_production_db` が本番 DB への接続を RuntimeError で止める）
  - LINE 送信系（`reply_line_message` / `send_line_message` /
    `broadcast_*` / `send_actionable_notification`）は全て差し替え。
    `requests.post` と `_enqueue_outbox`（`data/line_outbox.jsonl` に追記する）は
    呼ばれたら即 AssertionError にして、取り逃しを検出する
  - Matter は `lock_manager._matter_set_power` を差し替え。`aiohttp.ClientSession` も
    呼ばれたら AssertionError（**本物を叩くと祖母宅の炊飯器の電源が実際に動く**）

テストデータは全て架空。実在の個人名・LINE user ID・トークンは書かない。
"""
from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import json
import re
import sqlite3
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# --- 架空の識別子。実物とは一切関係ない ------------------------------------
SENDER_A = "Utest" + "a" * 27      # 登録済みの家族A
SENDER_B = "Utest" + "b" * 27      # 登録済みの家族B
STRANGER = "Utest" + "c" * 27      # 未登録の第三者
ADMIN_ID = "Utestadmin" + "0" * 22  # .env の LINE_USER_ID 相当（開発者本人の枠）
LINE_SECRET = "test-line-channel-secret-not-real"
RICE_NODE_ID = 7                    # .env の RICE_COOKER_NODE_ID を上書きした値

# `seeded_db` が作る persons: 0=未確定(センチネル) / 1=テスト祖母 / 2=母 / 3=テスト家族
# （id=2 '母' は init_db の既定シードが入れる。LINE の「母さん」ボタンは
#   person_id=2 を直書きするので、この並びが前提になっている）
GRANDMA_ID = 1


# ============================================================
# 小道具
# ============================================================
class Db:
    """一時 DB を直接読み書きするための最小ヘルパ（本番コードは経由しない）。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def q(self, sql: str, *args) -> list[dict]:
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def one(self, sql: str, *args) -> dict | None:
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def scalar(self, sql: str, *args):
        row = self.one(sql, *args)
        return None if row is None else list(row.values())[0]

    def w(self, sql: str, *args) -> int:
        conn = self._conn()
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        """LINE から触れるテーブルの行数。黙って消えていないかの比較用。"""
        tables = (
            "persons", "events", "meal_sessions", "session_events", "meal_photos",
            "family_line_users", "pending_notifications", "rice_classifications",
            "bath_classifications", "device_state", "edit_log",
        )
        return {t: self.scalar(f"SELECT COUNT(*) FROM {t}") for t in tables}


def run(coro):
    """postback ハンドラは async。中で asyncio.to_thread を使うのでループが必要。"""
    return asyncio.run(coro)


def jst(minutes_ago: int = 0) -> str:
    """`events.started_at` / `meal_sessions.started_at` と同じ JST の素朴な文字列。"""
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _no_network(*args, **kwargs):
    raise AssertionError(
        "テストが外部へ出ようとした（LINE API / Matter）。差し替えが漏れている。"
    )


@pytest.fixture
def st(frozen_now, seeded_db, monkeypatch):
    """LINE の postback / コマンドを安全に実行できる状態を作る。

    返すもの:
      st.db            一時 DB ヘルパ
      st.matter        Matter に送った ("on"/"off", node_id) の記録
      st.matter_ok     False にすると Matter 失敗を模擬できる
      st.broadcasts    全家族への broadcast 本文
      st.pushes        個別 push の (宛先, 本文)
      st.lc / st.notifier / st.lock_manager / st.db_module  対象モジュール
    """
    import src.db as db_module
    import src.line_commands as lc
    import src.lock_manager as lock_manager
    import src.notifier as notifier
    import src.sessions as sessions

    matter: list[tuple[str, int]] = []
    broadcasts: list[str] = []
    pushes: list[tuple[str, str]] = []
    flags = {"matter_ok": True}

    async def fake_power(node_id: int, on: bool) -> bool:
        matter.append(("on" if on else "off", node_id))
        return flags["matter_ok"]

    monkeypatch.setattr(lock_manager, "_matter_set_power", fake_power)
    # 差し替え漏れがあっても実機に届かないよう二重に塞ぐ
    monkeypatch.setattr(lock_manager.aiohttp, "ClientSession", _no_network)
    monkeypatch.setattr(notifier.requests, "post", _no_network)
    monkeypatch.setattr(notifier, "_enqueue_outbox", _no_network)

    monkeypatch.setattr(
        notifier, "broadcast_line_message",
        lambda message: (broadcasts.append(message), 1)[1],
    )
    monkeypatch.setattr(
        notifier, "send_line_message",
        lambda message, user_id=None: (pushes.append((user_id or "", message)), True)[1],
    )
    monkeypatch.setattr(
        notifier, "send_line_with_quick_reply",
        lambda message, items, user_id=None: (pushes.append((user_id or "", message)), True)[1],
    )
    monkeypatch.setattr(
        notifier, "broadcast_with_quick_reply",
        lambda message, items: (broadcasts.append(message), 1)[1],
    )
    monkeypatch.setattr(notifier, "reply_line_message", lambda token, message: True)
    monkeypatch.setattr(notifier, "send_actionable_notification",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("このテストで actionable 通知は想定外")))
    monkeypatch.setattr(notifier, "notify_device_locked", lambda *a, **k: True)
    monkeypatch.setattr(notifier, "notify_device_unlocked", lambda *a, **k: True)
    monkeypatch.setattr(notifier, "notify_meal_alert", lambda *a, **k: True)

    # .env を読ませない（本番のトークン・実名をテストに持ち込まない）
    monkeypatch.setattr(notifier, "_load_env", lambda: {
        "LINE_USER_ID": ADMIN_ID,
        "LINE_ALLOWED_SENDERS": "",
        "LINE_CHANNEL_ACCESS_TOKEN": "test-token-not-real",
    })
    monkeypatch.setattr(notifier, "_admin_display_name", lambda: "テスト管理者")
    monkeypatch.setenv("RICE_COOKER_NODE_ID", str(RICE_NODE_ID))

    # プロセス内メモリの確認コード／ロックアウトはテスト間で持ち越す
    lc._pending_unlocks.clear()
    lc._lockouts.clear()

    yield types.SimpleNamespace(
        db=Db(seeded_db), matter=matter, broadcasts=broadcasts, pushes=pushes,
        flags=flags, lc=lc, notifier=notifier, lock_manager=lock_manager,
        sessions=sessions, db_module=db_module,
    )

    lc._pending_unlocks.clear()
    lc._lockouts.clear()


@pytest.fixture
def registered(st):
    """家族A・家族Bを登録済みにする（LINE の「登録」コマンドそのものを使う）。"""
    st.lc.handle_register_family("登録 A家族", SENDER_A)
    st.lc.handle_register_family("登録 B家族", SENDER_B)
    return st


# ---- シナリオ作成 ---------------------------------------------------------
def make_event(st, *, person_id=GRANDMA_ID, source="rice_cooker",
               event_type="power_on", started_at=None, value=700.0) -> int:
    return st.db.w(
        "INSERT INTO events(person_id, source, event_type, started_at, value)"
        " VALUES(?, ?, ?, ?, ?)",
        person_id, source, event_type, started_at or jst(30), value,
    )


def make_session(st, *, person_id=GRANDMA_ID, label="昼食", confirmed=1,
                 started_at=None, minutes=20, event_ids=()) -> int:
    start = started_at or jst(30)
    end = (datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
           + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    sid = st.db.w(
        "INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count,"
        " label, confirmed) VALUES(?, ?, ?, ?, ?, ?)",
        person_id, start, end, len(event_ids) or 1, label, confirmed,
    )
    for eid in event_ids:
        st.db.w("INSERT INTO session_events(session_id, event_id) VALUES(?, ?)", sid, eid)
    return sid


def make_pending(st, notification_type: str, context_key: str) -> int:
    return st.db.w(
        "INSERT INTO pending_notifications(notification_type, context_key, message)"
        " VALUES(?, ?, ?)",
        notification_type, context_key, "テスト通知",
    )


def make_photo(st, session_id: int) -> int:
    """食事写真の DB 行。ファイル名は架空（本番の data/meal_photos/ は触らない）。"""
    return st.db.w(
        "INSERT INTO meal_photos(session_id, person_id, file_name, taken_at)"
        " VALUES(?, ?, ?, ?)",
        session_id, GRANDMA_ID, "test-not-a-real-photo.jpg", jst(25),
    )


# ============================================================
# 1. LINE から到達できる破壊的操作の全列挙
# ============================================================
# `src/line_commands.py` の中の書き込み SQL と、そこへの到達経路。
#   (種別, テーブル): 件数
# 増えたらこのテストが落ちる。落ちたら「誰がどの文言で到達できるか」を
# 確認してこの表と下の到達経路コメントを更新すること。
#
#   DELETE family_line_users  ← テキスト「登録解除」1通（自分の行のみ）
#   DELETE session_events     ← postback `rice_action:<event_id>:keep_warm|lid_only`
#   DELETE events             ← 同上（**中身を確認せず1行消す**）
#   DELETE meal_sessions      ← 同上（イベントが0件になったセッション / CASCADE で meal_photos も）
#   UPDATE pending_notifications ← 同上（当日の rice_action 通知を一括クローズ）
#   UPDATE meal_sessions x3   ← postback `sess_confirm:<sid>:reject` / `sess_confirm:<sid>:<person>`
#                               / `attribute:<sid>:<person_id>`
#   UPDATE events             ← postback `attribute:<sid>:<person_id>`
#   UPDATE bath_classifications ← postback `bath_cls:<record_id>:<choice>`
#   INSERT persons            ← テキスト「登録 <名前>」（未登録の第三者でも通る）
#   INSERT family_line_users  ← 同上
#   INSERT feedback           ← テキスト「意見/質問/要望/バグ <本文>」
#   INSERT rice_classifications ← postback `rice_action:<event_id>:<action>`
# さらに `src/sessions.py` の `merge_sessions_manual`（postback `merge:`）が
# meal_sessions / session_events / events を書き換えて1行消す。
MUTATING_SQL: dict[tuple[str, str], int] = {
    ("DELETE", "events"): 1,
    ("DELETE", "family_line_users"): 1,
    ("DELETE", "meal_sessions"): 1,
    ("DELETE", "session_events"): 1,
    ("INSERT", "family_line_users"): 1,
    ("INSERT", "feedback"): 1,
    ("INSERT", "persons"): 1,
    ("INSERT", "rice_classifications"): 1,
    ("UPDATE", "bath_classifications"): 1,
    ("UPDATE", "events"): 1,
    ("UPDATE", "meal_sessions"): 3,
    ("UPDATE", "pending_notifications"): 1,
}

_SQL_RE = re.compile(
    r"\b(DELETE\s+FROM|UPDATE|INSERT\s+INTO)\s+(?:OR\s+\w+\s+)?([a-z_][a-z0-9_]*)", re.I
)


def test_line_commandsの書き込みSQLの一覧を固定する():
    """破壊的 SQL が1本増えたら気づけるようにする。

    LINE のボタンは家族のトーク履歴に残り続けるので、新しい書き込みを
    1本足すだけで「古いボタンを押したら本番データが消える」経路が増える。
    レビューで到達経路を決めない限り増やせない、というのがこのテストの仕事。
    """
    src = (ROOT / "src" / "line_commands.py").read_text()
    found: collections.Counter = collections.Counter()
    for m in _SQL_RE.finditer(src):
        kind = m.group(1).upper().split()[0]
        table = m.group(2)
        if table.upper() == "SET":   # ON CONFLICT ... DO UPDATE SET
            continue
        found[(kind, table)] += 1
    assert dict(found) == MUTATING_SQL, (
        "line_commands.py の書き込み SQL が変わった。"
        "どのコマンド/postback から到達するかを確認して MUTATING_SQL を更新すること。"
    )


MESSAGE_SAMPLES = [
    "状況", "様子はどう", "今日はどう", "最後の食事", "さっきの食事",
    "ヘルプ", "help", "コマンド", "使い方", "メニュー", "menu",
    "登録一覧", "メンバー", "誰が登録",
    "リンク", "URL", "つながらない", "見れない", "アクセス",
    "ロック解除", "解除", "アンロック", "1234",
    "意見 テストの意見", "質問 テストの質問", "要望 テスト", "バグ テスト",
    "削除", "消して", "全部消して", "DELETE", "drop table events",
    "ありがとう", "", "　", "😊",
]


def test_テキストメッセージ1通では祖母の記録を消せない(st):
    """「LINE メッセージ1通でイベントやセッションを消せるのか」への答え。

    消せない。`dispatch()` は postback ハンドラへ一切ルーティングしないので、
    events / meal_sessions / session_events / meal_photos を消す経路は
    テキストからは存在しない（消せるのは後述の「登録解除」= 自分の家族登録行だけ）。
    将来 dispatch に postback 相当を足したらここが落ちる。
    """
    eid = make_event(st)
    sid = make_session(st, event_ids=[eid])
    make_photo(st, sid)
    make_pending(st, "meal_alert", "2026-10-15_1")
    st.lc.handle_register_family("登録 A家族", SENDER_A)
    before = st.db.counts()

    for text in MESSAGE_SAMPLES:
        run(st.lc.dispatch(text, SENDER_A))

    after = st.db.counts()
    for table in ("events", "meal_sessions", "session_events", "meal_photos",
                  "pending_notifications", "bath_classifications"):
        assert after[table] == before[table], f"{table} がテキストメッセージで変化した"
    assert st.matter == [], "テキストメッセージで Matter を叩いた"


def test_登録解除はテキスト1通で家族登録行を消す(st):
    """テキスト1通で消せる唯一のもの。自分の `family_line_users` 行。

    `persons` 行は残るので「登録解除 → 再登録」で同じ人物に戻れる。
    逆に、家族管理画面の人物一覧には抜けた人の行が残り続ける。
    """
    st.lc.handle_register_family("登録 おじ", SENDER_A)
    assert st.db.scalar("SELECT COUNT(*) FROM family_line_users") == 1
    person_id = st.db.scalar("SELECT person_id FROM family_line_users")

    reply = run(st.lc.dispatch("登録解除", SENDER_A))

    assert "解除しました" in reply
    assert st.db.scalar("SELECT COUNT(*) FROM family_line_users") == 0
    assert st.db.one("SELECT name FROM persons WHERE id = ?", person_id)["name"] == "おじ", \
        "persons 行は残る（通知先からは外れるが人物一覧には残る）"


# ============================================================
# 2. rice_action: LINE の1タップで消えるもの
# ============================================================
def test_保温ボタンは炊飯器と無関係なイベントも中身を見ずに消す(st):
    """`rice_action:<event_id>:keep_warm` が削除前に何も検査していないことの実測。

    押されたボタンの event_id が本当に炊飯器の power_on かどうかを確認しないため、
    お風呂の検知イベントでも消える。しかも
      events 削除 → session_events → meal_sessions → **meal_photos が CASCADE**
    まで連鎖する（`get_conn()` が foreign_keys を ON にしているため）。
    家族は「保温でした」と答えただけで、祖母のお風呂の記録と食事写真の行を失う。

    本来: event_id の source/event_type が炊飯器のものか確認し、
    削除ではなく「食事として数えない」フラグ（例 events.raw_meta や
    meal_sessions.confirmed=-1）で表現すべき。写真を道連れにしてはいけない。
    """
    bath_event = make_event(st, source="bath_humidity", event_type="presence",
                            started_at=jst(90), value=88.0)
    bath_session = make_session(st, label="お風呂", confirmed=1,
                                started_at=jst(90), event_ids=[bath_event])
    photo = make_photo(st, bath_session)
    assert st.db.one("SELECT id FROM meal_photos WHERE id = ?", photo)

    reply = run(st.lc.handle_rice_action_postback(
        f"rice_action:{bath_event}:keep_warm", SENDER_A))

    assert "保温" in reply
    assert st.db.one("SELECT id FROM events WHERE id = ?", bath_event) is None, \
        "お風呂の検知イベントが消えた"
    assert st.db.one("SELECT id FROM meal_sessions WHERE id = ?", bath_session) is None, \
        "お風呂のセッションが消えた"
    assert st.db.one("SELECT id FROM meal_photos WHERE id = ?", photo) is None, \
        "食事写真の行まで CASCADE で消えた"


def test_保温ボタンの削除は監査ログを残さない(st):
    """家族UIの削除は `edit_log` に残るのに、LINE の削除は何も残らない。

    `POST /api/events/{id}` (app.py の api_delete_event) は before_json 込みで
    edit_log に書く。LINE の保温ボタンは同じ行を消すのに記録を残さないので、
    「いつ誰が何を消したか」を後から追えない。

    本来: LINE 経由の削除も edit_log に残す（または論理削除にする）。
    直したら `edit_log` に1行入ることを assert する形に書き換える。
    """
    eid = make_event(st)
    sid = make_session(st, event_ids=[eid])

    run(st.lc.handle_rice_action_postback(f"rice_action:{eid}:lid_only", SENDER_A))

    assert st.db.one("SELECT id FROM events WHERE id = ?", eid) is None
    assert st.db.scalar("SELECT COUNT(*) FROM edit_log") == 0, \
        "現状固定: LINE 経由の削除は監査ログに残らない"
    assert st.db.one("SELECT id FROM meal_sessions WHERE id = ?", sid) is None


def test_保温ボタンは残ったイベントがあればセッションを残すがevent_countがずれる(st):
    """セッションに他のイベントが残れば削除はされない。ただし `event_count` は直さない。

    家族UI・LINE通知はどちらも「センサー反応 N件」を event_count から出すので、
    削除後は実際の件数と表示が合わなくなる（1件消しても N のまま）。

    本来: event_count を再計算するか、表示側が session_events を数える。
    """
    keep = make_event(st, started_at=jst(40))
    drop = make_event(st, started_at=jst(35))
    sid = make_session(st, event_ids=[keep, drop])
    st.db.w("UPDATE meal_sessions SET event_count = 2 WHERE id = ?", sid)

    run(st.lc.handle_rice_action_postback(f"rice_action:{drop}:keep_warm", SENDER_A))

    assert st.db.one("SELECT id FROM meal_sessions WHERE id = ?", sid), "セッションは残る"
    assert st.db.scalar(
        "SELECT COUNT(*) FROM session_events WHERE session_id = ?", sid) == 1
    assert st.db.scalar("SELECT event_count FROM meal_sessions WHERE id = ?", sid) == 2, \
        "現状固定: event_count は削除後も古い値のまま"


def test_保温ボタンは当日の無関係な炊飯器通知も一括で閉じる(st):
    """1タップで「同種の未回答通知」を全部クローズする副作用。

    `UPDATE pending_notifications WHERE notification_type='rice_action'
       AND completed_at IS NULL AND created_at >= datetime('now','-24 hours')`
    は context_key を見ないので、別のイベントについて家族に聞いている質問まで
    「同日の保温分類により自動完了」として閉じる。閉じられた質問は
    recheck_pending の再通知対象から外れ、二度と聞かれない。

    本来: 同じ炊飯器セッション（近接時刻 / 同一 event 由来）に限定する。
    """
    eid = make_event(st)
    other = make_pending(st, "rice_action", "event_99999")
    mine = make_pending(st, "rice_action", f"event_{eid}")
    meal = make_pending(st, "meal_alert", "2026-10-15_1")

    reply = run(st.lc.handle_rice_action_postback(
        f"rice_action:{eid}:keep_warm", SENDER_A))

    assert "他の同種 1件の通知も同時に閉じました" in reply
    assert st.db.scalar(
        "SELECT completed_action FROM pending_notifications WHERE id = ?", other
    ) == "同日の保温分類により自動完了（保温）", "無関係な炊飯器通知が閉じられた"
    assert st.db.scalar(
        "SELECT completed_at IS NOT NULL FROM pending_notifications WHERE id = ?", mine) == 1
    assert st.db.scalar(
        "SELECT completed_at IS NULL FROM pending_notifications WHERE id = ?", meal) == 1, \
        "別カテゴリは閉じない"


def test_消えたイベントの古いボタンでも対応済み通知が飛び未回答通知が閉じる(st):
    """トーク履歴を遡って古い「保温」ボタンを押したときの実測。

    event_id が既に存在しなくてもハンドラは止まらず、
      - 学習データ（rice_classifications）は作られない
      - なのに「☑️ ○○さんが対応しました」が全家族に broadcast される
      - 当日の本物の未回答 rice_action 通知が巻き込まれて閉じる
    という状態になる。「反応するときとしないときがある」の中で
    「押したのに何も直っていない」側の説明になりうる。

    本来: 対象イベントが無ければ「この通知は古いので操作できません」と返して
    何も書かない。
    """
    live = make_pending(st, "rice_action", "event_777")

    reply = run(st.lc.handle_rice_action_postback(
        "rice_action:99999:keep_warm", SENDER_A))

    assert "保温" in reply
    assert st.db.scalar("SELECT COUNT(*) FROM rice_classifications") == 0, \
        "学習データは残らない（power_w が取れないため）"
    assert len(st.broadcasts) == 1 and "対応しました" in st.broadcasts[0]
    assert st.db.scalar(
        "SELECT completed_at IS NOT NULL FROM pending_notifications WHERE id = ?", live) == 1, \
        "本物の未回答通知が、存在しないイベントのボタンで閉じられた"


def test_未知のactionはCHECK制約で学習に残らないのに判定保留と返る(st):
    """`rice_action:<id>:<未知>` の実測。

    rice_classifications.classification の CHECK に落ちて保存は失敗するが、
    例外は warning ログに飲まれ、家族には「判定保留にしました（次回似た動きが
    来たら学習します）」と返る。学習されていないのに学習したと伝わる。
    """
    eid = make_event(st)

    reply = run(st.lc.handle_rice_action_postback(
        f"rice_action:{eid}:bogus_action", SENDER_A))

    assert "判定保留" in reply
    assert st.db.scalar("SELECT COUNT(*) FROM rice_classifications") == 0
    assert st.db.one("SELECT id FROM events WHERE id = ?", eid), "イベントは消えない"


def test_cookボタンはイベントを消さないが食事回数にも入らない(st):
    """「炊飯」と答えると『祖母さんの食事1回として記録』と返るが、実際には数えない。

    `meal_sessions.confirmed` が 0 のまま残り、`sessions_today()` の既定
    （confirmed=1 のみ）に出ないため「状況」の食事回数は 0 のままになる。
    家族は答えたのに祖母の画面と家族の集計に反映されない。

    本来: cook/lid_meal を選んだら confirmed=1 / person_id を確定させる。
    直したら「食事回数: 1回」と sessions_today に1件出ることを assert する。
    """
    eid = make_event(st, started_at=jst(45))
    sid = make_session(st, label="昼食", confirmed=0, started_at=jst(45), event_ids=[eid])

    reply = run(st.lc.handle_rice_action_postback(f"rice_action:{eid}:cook", SENDER_A))

    assert "食事1回として記録" in reply
    assert st.db.one("SELECT id FROM events WHERE id = ?", eid), "cook ではイベントを消さない"
    assert st.db.scalar("SELECT confirmed FROM meal_sessions WHERE id = ?", sid) == 0, \
        "現状固定: confirmed は 0 のまま"
    assert st.sessions.sessions_today(GRANDMA_ID) == [], "食事として数えられていない"
    assert "🍴 食事回数: 0回" in st.lc.handle_status()


# ============================================================
# 3. 家族登録フロー（persons / family_line_users）
# ============================================================
def test_登録でpersonsとfamily_line_usersに書かれる内容(st):
    """「登録 <名前>」が書く行の実測。新しい名前なら persons に1行増える。"""
    before = st.db.counts()

    reply = st.lc.handle_register_family("登録 おば", SENDER_A)

    assert "「おば」として登録しました" in reply
    assert "新しい人物として作成しました" in reply
    assert st.db.counts()["persons"] == before["persons"] + 1
    person = st.db.one("SELECT id, name, role FROM persons WHERE name = 'おば'")
    assert person["role"] == "family"
    row = st.db.one(
        "SELECT person_id, display_name, registered_at, last_seen_at"
        " FROM family_line_users WHERE line_user_id = ?", SENDER_A)
    assert row["person_id"] == person["id"]
    assert row["display_name"] == "おば"
    assert row["registered_at"] and row["last_seen_at"]
    # 登録した本人がそのまま通知先と許可送信者になる
    assert SENDER_A in st.notifier._load_recipients()


def test_既存の名前で登録すると既存の人物行に乗っ取れる(st):
    """`SELECT id FROM persons WHERE name = ?` で一致しただけで紐づける実測。

    第三者が「登録 テスト祖母」と送ると、祖母本人の persons 行（role='grandma'）に
    自分の LINE が結びつく。以後 `resolve_confirmer_name` はその送信者を
    「テスト祖母」として解決するので、全家族に流れる
    「☑️ テスト祖母さんが対応しました」が誰の操作か分からなくなる
    （祖母本人は LINE を使わないので、見分けがつかない）。

    本来: 既存人物への紐付けは家族の承認を要求する。少なくとも
    role='grandma' の行と、既に別の LINE が紐づいている行は選ばせない。
    """
    reply = st.lc.handle_register_family("登録 テスト祖母", STRANGER)

    assert "「テスト祖母」として登録しました" in reply
    assert "新しい人物として作成しました" not in reply
    assert st.db.scalar(
        "SELECT person_id FROM family_line_users WHERE line_user_id = ?", STRANGER
    ) == GRANDMA_ID, "祖母本人の人物行に第三者の LINE が紐づいた"
    assert st.notifier.resolve_confirmer_name(STRANGER) == "テスト祖母"


def test_同じLINEユーザが別名で登録すると人物行が増え古い行が残る(st):
    """再登録の実測。`family_line_users` は上書きされるが `persons` は増え続ける。

    「登録 母」→「登録 お母さん」と送り直すと、persons に2行できて
    古い行は誰も紐づかないまま残る（家族管理画面の人物一覧・attribute の
    Quick Reply ボタンにはその幽霊人物が並び続ける）。
    """
    st.lc.handle_register_family("登録 母A", SENDER_A)
    first = st.db.scalar("SELECT id FROM persons WHERE name = '母A'")

    reply = st.lc.handle_register_family("登録 母B", SENDER_A)

    assert "前の登録を上書きしました" in reply
    assert st.db.scalar(
        "SELECT COUNT(*) FROM family_line_users WHERE line_user_id = ?", SENDER_A) == 1
    assert st.db.scalar(
        "SELECT person_id FROM family_line_users WHERE line_user_id = ?", SENDER_A
    ) == st.db.scalar("SELECT id FROM persons WHERE name = '母B'")
    assert st.db.one("SELECT id FROM persons WHERE id = ?", first), \
        "現状固定: 使われなくなった人物行が残る"


def test_名前の空白以降は黙って捨てられる(st):
    """`text.split()` の 2 番目だけを採るので、名前に空白を入れると切れる。

    「登録 山田 太郎」は「山田」として登録され、家族には
    「✅ 「山田」として登録しました」と返るだけ。切り捨てた旨の説明は無い。
    全角空白も同じ（事前に半角へ置換されるため）。

    本来: 2 番目以降を全部つなげる（`text.split(maxsplit=1)[1]`）か、
    空白を含む名前は明示的に断る。
    """
    st.lc.handle_register_family("登録 山田 太郎", SENDER_A)
    assert st.db.scalar(
        "SELECT display_name FROM family_line_users WHERE line_user_id = ?", SENDER_A
    ) == "山田"

    st.lc.handle_register_family("登録　佐藤　花子", SENDER_B)
    assert st.db.scalar(
        "SELECT display_name FROM family_line_users WHERE line_user_id = ?", SENDER_B
    ) == "佐藤"


def test_名前の絵文字とSQL特殊文字はそのまま保存される(st):
    """プレースホルダ渡しなので SQL インジェクションは成立しない（良い挙動の固定）。

    ただし絵文字や記号もそのまま人物名になるので、家族管理画面と
    Quick Reply のラベルにそのまま出る。
    """
    st.lc.handle_register_family("登録 👩‍🦳", SENDER_A)
    st.lc.handle_register_family("登録 '); DROP TABLE events; --", SENDER_B)

    assert st.db.scalar("SELECT COUNT(*) FROM events") == 0  # そもそも空
    assert st.db.one("SELECT name FROM persons WHERE name = '👩‍🦳'")
    # split() で最初の語だけが名前になる
    assert st.db.one("SELECT name FROM persons WHERE name = ''');'")
    assert st.db.scalar(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='events'") == 1


@pytest.mark.parametrize("text, expect_written", [
    ("登録", False),
    ("登録　", False),
    ("登録 " + "あ" * 21, False),
    ("登録 " + "あ" * 1000, False),
    ("登録 未確定", False),
    ("登録 " + "あ" * 20, True),
])
def test_登録できない名前は何も書き込まない(st, text, expect_written):
    """長さ上限（20文字）・予約名・名前なしの実測。書かれるか書かれないかを固定する。"""
    before = st.db.counts()

    st.lc.handle_register_family(text, SENDER_A)

    after = st.db.counts()
    if expect_written:
        assert after["persons"] == before["persons"] + 1
        assert after["family_line_users"] == before["family_line_users"] + 1
    else:
        assert after["persons"] == before["persons"], f"{text!r} で persons が増えた"
        assert after["family_line_users"] == before["family_line_users"]


def test_2人が同じ名前で登録すると同じ人物行を共有する(st):
    """別の LINE ユーザが同じ名前で登録したときの実測。

    `persons.name` が UNIQUE なので、2人目は1人目の人物行に相乗りする。
    結果「登録一覧」には同じ名前が2行並び、broadcast の
    「☑️ ○○さんが対応しました」はどちらの操作か区別できない。
    """
    st.lc.handle_register_family("登録 おじ", SENDER_A)
    st.lc.handle_register_family("登録 おじ", SENDER_B)

    assert st.db.scalar("SELECT COUNT(*) FROM persons WHERE name = 'おじ'") == 1
    ids = [r["person_id"] for r in st.db.q("SELECT person_id FROM family_line_users")]
    assert ids[0] == ids[1]
    listing = st.lc.handle_list_registered()
    assert listing.count("おじ") == 2
    assert st.notifier.resolve_confirmer_name(SENDER_A) == \
        st.notifier.resolve_confirmer_name(SENDER_B) == "おじ"


def test_登録解除しても通知先から外れるのはDB登録分だけ(st, monkeypatch):
    """「通知は届かなくなります」が嘘になる条件の実測。

    `_load_recipients()` は `.env` の LINE_ALLOWED_SENDERS / LINE_USER_ID と
    DB の family_line_users を**和集合**で作る。つまり .env 側に書かれている
    宛先（開発者本人＝LINE_USER_ID、過去に .env に追記された家族）は
    「登録解除」しても通知が届き続ける。
    （実機の .env は LINE_ALLOWED_SENDERS が空なので、現状これに該当するのは
      LINE_USER_ID の1件。.env に家族を足した瞬間に効きだす。）

    本来: 解除は「通知を止める」ことを意味するので、.env 由来の宛先も
    除外リスト（DB）で上書きできるようにする。
    """
    monkeypatch.setattr(st.notifier, "_load_env", lambda: {
        "LINE_USER_ID": ADMIN_ID,
        "LINE_ALLOWED_SENDERS": SENDER_B,
        "LINE_CHANNEL_ACCESS_TOKEN": "test-token-not-real",
    })
    st.lc.handle_register_family("登録 A家族", SENDER_A)
    st.lc.handle_register_family("登録 B家族", SENDER_B)

    assert "通知は届かなくなります" in st.lc.handle_unregister_family(SENDER_A)
    assert "通知は届かなくなります" in st.lc.handle_unregister_family(SENDER_B)

    recipients = st.notifier._load_recipients()
    assert SENDER_A not in recipients, "DB 登録のみの人は通知先から外れる"
    assert SENDER_B in recipients, "現状固定: .env に書かれた宛先は解除後も通知先に残る"
    assert ADMIN_ID in recipients


def test_未登録の第三者が自分で登録すると許可送信者と通知先になる(st, monkeypatch, tmp_path):
    """承認ステップが無いことの実測。

    許可リストチェックは「登録」コマンドだけ未登録者に通す設計で、その1通が
    `family_line_users` に行を作る。行が入った瞬間に
      - `_load_line_allowed_senders()` に入る（= 以後すべてのコマンドが通る）
      - `_load_recipients()` に入る（= 祖母の生活通知が全部届く）
    の両方が自動で成立する。家族の承認も、開発者への通知も無い。

    本来: 登録は申請として記録し、既存家族の承認（または admin への確認）を
    経てから有効化する。
    """
    import src.web.app as app_module
    # 本番 .env を読ませない。BASE.parent.parent/.env を見るので架空の .env を置く
    fake_root = tmp_path / "fake_repo"
    (fake_root / "src" / "web").mkdir(parents=True)
    (fake_root / ".env").write_text(
        f"LINE_USER_ID={ADMIN_ID}\nLINE_ALLOWED_SENDERS=\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "BASE", fake_root / "src" / "web")

    assert app_module._load_line_allowed_senders() == {ADMIN_ID}
    assert STRANGER not in app_module._load_line_allowed_senders()

    st.lc.handle_register_family("登録 親戚", STRANGER)

    assert STRANGER in app_module._load_line_allowed_senders(), \
        "1通で許可送信者になった"
    assert STRANGER in st.notifier._load_recipients(), "1通で通知先になった"


def test_登録解除は自分の行だけを消す(st):
    """他人の登録を消せないことの確認（良い挙動の固定）。

    `DELETE FROM family_line_users WHERE line_user_id = ?` に送信者自身の ID
    しか渡さないので、他の家族の登録には触れない。
    """
    st.lc.handle_register_family("登録 A家族", SENDER_A)
    st.lc.handle_register_family("登録 B家族", SENDER_B)

    st.lc.handle_unregister_family(SENDER_A)

    remaining = [r["line_user_id"] for r in st.db.q("SELECT line_user_id FROM family_line_users")]
    assert remaining == [SENDER_B]
    assert "まだ登録されていません" in st.lc.handle_unregister_family(SENDER_A)


# ============================================================
# 4. 炊飯器の電源（Matter）と device_state
# ============================================================
# LINE から Matter の電源操作に到達する経路は3つ:
#   postback `lock_confirm:<ctx>:yes`      → lock_device   (node_id = .env RICE_COOKER_NODE_ID)
#   postback `attribute:<sid>:1`（祖母）   → lock_device   (node_id = 1 の直書き)
#   テキスト「ロック解除」→ 4桁コード返信 → unlock_device (node_id = 1 の直書き)
def test_lock_confirmのyesで実際にMatterへOFFが出てDBがロック済みになる(st):
    """家族が「ロックする」を押したときの正常系。電源 OFF と device_state を固定する。"""
    reply = run(st.lc.handle_lock_confirm_postback("lock_confirm:2026-10-15_1200:yes", SENDER_A))

    assert "炊飯器をロックしました" in reply
    assert st.matter == [("off", RICE_NODE_ID)], "lock_confirm は .env の node_id を使う"
    state = st.db.one("SELECT * FROM device_state WHERE device_name = 'rice_cooker'")
    assert state["is_locked"] == 1
    assert state["cycle_count_today"] == 1
    assert state["unlock_due_at"], "期限が入る（cron の自動解除がこれを見る）"


def test_lock_confirmのnoはMatterもDBも触らない(st):
    """「ロックしない」を押したときに副作用が無いことの確認。"""
    reply = run(st.lc.handle_lock_confirm_postback("lock_confirm:2026-10-15_1200:no", SENDER_A))

    assert "スキップ" in reply
    assert st.matter == []
    assert st.db.scalar("SELECT COUNT(*) FROM device_state") == 0


def test_Matterが失敗したときDBはロック済みにならない(st):
    """Matter 側のエラーで「DB だけロック済み」にならないことの確認（良い挙動の固定）。

    `lock_device` は `_matter_set_power` が True を返したときだけ
    device_state を更新する。失敗時は行が作られるだけ（is_locked=0）。
    """
    st.flags["matter_ok"] = False

    reply = run(st.lc.handle_lock_confirm_postback("lock_confirm:2026-10-15_1200:yes", SENDER_A))

    assert "ロック実行に失敗" in reply
    assert st.matter == [("off", RICE_NODE_ID)]
    assert st.db.scalar(
        "SELECT is_locked FROM device_state WHERE device_name = 'rice_cooker'") == 0


def test_Matter成功後にDB書込が失敗すると電源だけ切れて復旧経路が無い(st, monkeypatch):
    """逆方向の失敗。**炊飯器の電源だけが切れて、どこからも戻せなくなる。**

    `lock_device` は「Matter へ OFF を送る」→「device_state を UPDATE」の順で、
    2つ目が失敗しても1つ目は取り消せない。DB 書き込みは "database is locked"
    （このシステムは1つの SQLite を monitor / web / cron 5本で共有している）で
    実際に失敗しうる。そのとき:
      - 家族への返信は「ロック実行に失敗しました（matter通信エラー）」= 嘘
      - device_state は is_locked=0 のままなので
        `release_expired_locks()`（cron 5分毎）は `is_locked = 1` しか見ず回収しない
      - 家族が「ロック解除」と送っても「🔓 既にロックされていません」で終わる
    結果、祖母の炊飯器は物理的に電源が切れたまま誰も戻せない
    （2026-08-30 の50時間ロックと同じ形）。

    本来: Matter 成功後の DB 書き込みが失敗したら、その場で電源を戻す
    （補償）か、先に「ロック予定」を DB に書いてから電源を切る。
    """
    import src.lock_manager as lock_manager
    real_ensure = lock_manager._ensure_device
    state = {"fail": False}

    def flaky_transaction(*args, **kwargs):
        if state["fail"]:
            raise sqlite3.OperationalError("database is locked")
        return st.db_module.transaction(*args, **kwargs)

    def ensure_then_break(device_name):
        real_ensure(device_name)       # 行の用意までは成功させる
        state["fail"] = True           # 電源 OFF 後の UPDATE だけ失敗させる

    monkeypatch.setattr(lock_manager, "_ensure_device", ensure_then_break)
    monkeypatch.setattr(lock_manager, "transaction", flaky_transaction)

    reply = run(st.lc.handle_lock_confirm_postback("lock_confirm:2026-10-15_1200:yes", SENDER_A))
    state["fail"] = False

    assert st.matter == [("off", RICE_NODE_ID)], "電源は物理的に切れた"
    assert "ロック実行に失敗" in reply, "家族には失敗と伝わる"
    assert st.db.scalar(
        "SELECT is_locked FROM device_state WHERE device_name = 'rice_cooker'") == 0, \
        "DB は解除済みのまま"
    assert st.db.q("SELECT device_name FROM device_state WHERE is_locked = 1") == [], \
        "cron の期限解除の対象にならない"
    assert "既にロックされていません" in st.lc.handle_unlock_request(SENDER_A), \
        "家族が LINE から解除しようとしても受け付けられない"


def test_確認コードの返信でMatterへONが出てDBが解除される(st):
    """「ロック解除」→4桁コード→解除 の正常系。node_id=1 直書きの経路。"""
    st.db.w(
        "INSERT INTO device_state(device_name, is_locked, unlock_due_at)"
        " VALUES('rice_cooker', 1, ?)", jst(-60))

    issued = st.lc.handle_unlock_request(SENDER_A)
    code = re.search(r"確認コード: (\d{4})", issued).group(1)
    reply = run(st.lc.handle_unlock_confirm(code, SENDER_A))

    assert "ロックを解除しました" in reply
    assert st.matter == [("on", 1)], "解除は node_id=1 の直書き（lock 側は .env 由来で不一致）"
    state = st.db.one("SELECT is_locked, unlock_due_at FROM device_state")
    assert state["is_locked"] == 0
    assert state["unlock_due_at"] is None


def test_コードが違うときはMatterもDBも触らない(st):
    """総当たり対策の副作用確認。失敗カウントはプロセス内メモリだけに積む。"""
    st.db.w("INSERT INTO device_state(device_name, is_locked) VALUES('rice_cooker', 1)")
    issued = st.lc.handle_unlock_request(SENDER_A)
    code = re.search(r"確認コード: (\d{4})", issued).group(1)
    wrong = "0000" if code != "0000" else "1111"

    reply = run(st.lc.handle_unlock_confirm(wrong, SENDER_A))

    assert "コードが違います" in reply
    assert st.matter == []
    assert st.db.scalar("SELECT is_locked FROM device_state") == 1


def test_人物割当のボタンから炊飯器の電源が切れる(st):
    """`attribute:<sid>:1`（祖母）は人物の記録だけでなく電源操作まで行う。

    直近に確定済みの食事が2回以上あり、直近90分以内に食事があり、
    まだロックされていなければ、その場で Matter に OFF を送る。
    つまり「誰の食事ですか？」の **祖母ボタン1タップで祖母の炊飯器が止まる**。
    テストで Matter をスタブしないと本物の電源が落ちる経路。
    """
    make_session(st, label="昼食", confirmed=1, started_at=jst(70))
    make_session(st, label="間食", confirmed=1, started_at=jst(40))
    eid = make_event(st, person_id=0, started_at=jst(10))
    sid = make_session(st, person_id=0, label="間食", confirmed=0,
                       started_at=jst(10), minutes=5, event_ids=[eid])

    reply = run(st.lc.handle_attribute_postback(f"attribute:{sid}:{GRANDMA_ID}", SENDER_A))

    assert "炊飯器をロックしました" in reply
    assert st.matter == [("off", 1)], "attribute 経路は node_id=1 の直書き"
    assert st.db.scalar("SELECT is_locked FROM device_state") == 1


# ============================================================
# 5. 人物の付け替え（attribute / merge）
# ============================================================
def test_attributeは確定済みセッションを警告なく他人に付け替える(st):
    """家族確認済み（confirmed=1）の食事でも、ボタン1つで別人に変わる実測。

    上書き時に log.info を出すだけで、家族への確認も `confirmed_by` の更新も
    `original_person_id` / `edited_by` / `edited_at`（編集メタ列）への記録も無い。
    家族UIの編集は edit_log に残るので、LINE 経由だけ履歴が残らない。

    本来: 確定済みの付け替えは「○○さんの記録を書き換えますか？」を挟むか、
    少なくとも original_person_id と edit_log に残す。
    """
    eid = make_event(st, person_id=GRANDMA_ID)
    sid = make_session(st, person_id=GRANDMA_ID, confirmed=1, event_ids=[eid])

    reply = run(st.lc.handle_attribute_postback(f"attribute:{sid}:3", SENDER_A))

    assert "テスト家族" in reply
    row = st.db.one("SELECT person_id, confirmed, confirmed_by FROM meal_sessions WHERE id = ?", sid)
    assert row["person_id"] == 3, "祖母の食事が別人のものになった"
    assert row["confirmed_by"] is None, "誰が付け替えたかは残らない"
    ev = st.db.one("SELECT person_id, original_person_id, edited_by, edited_at FROM events WHERE id = ?", eid)
    assert ev["person_id"] == 3, "紐づくイベントの person_id も書き換わる"
    assert (ev["original_person_id"], ev["edited_by"], ev["edited_at"]) == (None, None, None)
    assert st.db.scalar("SELECT COUNT(*) FROM edit_log") == 0


def test_attributeは未確定セッションを確定にしないので食事回数に入らない(st):
    """「誰の行動ですか？」に答えても食事として数えられない実測。

    `attribute:` は person_id を書くだけで `confirmed` を触らないため、
    `sessions_today()` の既定（confirmed=1 のみ）から漏れ続ける。
    一方 `sess_confirm:` は confirmed=1 にする。**同じ「誰の食事か」を聞く
    2つの通知で、答えた後の結果が違う。**

    家族から見ると「祖母を選んだのに『状況』の食事回数が増えない」。
    祖母のタブレットのスタンプ、週次レポート、食べ過ぎ判定も同じ値を見る。

    本来: attribute でも confirmed=1 / confirmed_by / confirmed_at を書く。
    直したら sessions_today に1件出ることを assert する形に書き換える。
    """
    eid = make_event(st, person_id=0, started_at=jst(45))
    sid = make_session(st, person_id=0, label="昼食", confirmed=0,
                       started_at=jst(45), event_ids=[eid])

    run(st.lc.handle_attribute_postback(f"attribute:{sid}:{GRANDMA_ID}", SENDER_A))

    assert st.db.scalar("SELECT person_id FROM meal_sessions WHERE id = ?", sid) == GRANDMA_ID
    assert st.db.scalar("SELECT confirmed FROM meal_sessions WHERE id = ?", sid) == 0
    assert st.sessions.sessions_today(GRANDMA_ID) == [], "確定扱いにならないので出てこない"
    assert "🍴 食事回数: 0回" in st.lc.handle_status()
    assert len(st.sessions.sessions_today(GRANDMA_ID, include_unconfirmed=True)) == 1


def test_attributeで未確定センチネルに戻せる(st):
    """`attribute:<sid>:0`（「不明」ボタン）の実測。

    persons の id=0 は「未確定」センチネルなので存在チェックを通る。
    確定済みの食事に対して押すと、person_id が 0 に戻って
    `sessions_today(1)` から消える（祖母の食事が1回減る）。
    """
    eid = make_event(st, person_id=GRANDMA_ID, started_at=jst(45))
    sid = make_session(st, person_id=GRANDMA_ID, label="昼食", confirmed=1,
                       started_at=jst(45), event_ids=[eid])
    assert len(st.sessions.sessions_today(GRANDMA_ID)) == 1

    reply = run(st.lc.handle_attribute_postback(f"attribute:{sid}:0", SENDER_A))

    assert "未確定" in reply
    assert st.db.scalar("SELECT person_id FROM meal_sessions WHERE id = ?", sid) == 0
    assert st.sessions.sessions_today(GRANDMA_ID) == [], "祖母の食事が1回消えた"


def test_mergeは他人の食事を祖母の食事としてまとめられる(st):
    """`merge:<new>:<prev>` が人物を確認しないことの実測。

    「前と同じ食事」ボタンは、祖母の昼食（prev）と母の夕食（new）でも統合でき、
    `merge_sessions_manual` が `events.person_id` を prev 側の人物で
    **一括上書き**する。母の夕食は行ごと消え、祖母の食事として残る。
    食事回数は炊飯器の自動ロック判定の入力なので、祖母の食事が
    実際より多く見える＝不要なロックにつながる。

    本来: prev と new の person_id が違う（どちらも未確定でない）ときは拒否する。
    """
    grandma_ev = make_event(st, person_id=GRANDMA_ID, started_at=jst(300))
    prev = make_session(st, person_id=GRANDMA_ID, label="昼食", confirmed=1,
                        started_at=jst(300), event_ids=[grandma_ev])
    mother_ev = make_event(st, person_id=2, source="ih", started_at=jst(30))
    new = make_session(st, person_id=2, label="夕食", confirmed=1,
                       started_at=jst(30), event_ids=[mother_ev])

    reply = run(st.lc.handle_merge_postback(f"merge:{new}:{prev}", SENDER_A))

    assert "統合しました" in reply
    assert st.db.one("SELECT id FROM meal_sessions WHERE id = ?", new) is None, \
        "母の夕食セッションが消えた"
    assert st.db.scalar("SELECT person_id FROM events WHERE id = ?", mother_ev) == GRANDMA_ID, \
        "母のイベントが祖母のものに書き換わった"
    merged = st.db.one("SELECT person_id, label, event_count FROM meal_sessions WHERE id = ?", prev)
    assert merged["person_id"] == GRANDMA_ID
    assert merged["label"] == "昼食", "夕食が昼食に吸収される"
    assert merged["event_count"] == 2


def test_mergeは時刻が何時間離れていても1回の食事にまとめる(frozen_now, st):
    """統合後の時刻範囲の実測。4時間離れていても1セッションになる。

    `started_at`=min / `ended_at`=max で広げるだけなので、
    家族UIの表示も週次レポートも「4時間続いた昼食」になる。

    本来: 一定時間（例: セッション結合の閾値）を超えた統合は拒否する。
    """
    prev = make_session(st, label="昼食", confirmed=1, started_at=jst(300), minutes=20)
    new = make_session(st, label="間食", confirmed=1, started_at=jst(60), minutes=10)

    run(st.lc.handle_merge_postback(f"merge:{new}:{prev}", SENDER_A))

    row = st.db.one("SELECT started_at, ended_at FROM meal_sessions WHERE id = ?", prev)
    span = (datetime.fromisoformat(str(row["ended_at"]))
            - datetime.fromisoformat(str(row["started_at"]))).total_seconds() / 60
    assert span > 230, f"統合後のセッション長 = {span:.0f}分"


def test_mergeは新しい側の食事写真を消す(st):
    """統合で消える `meal_sessions` 行に紐づく写真が CASCADE で消える実測。

    家族が撮った直近の食事写真が「前と同じ食事」ボタンで DB から消える
    （ファイル自体は data/meal_photos/ に残るので、孤児ファイルになる）。

    本来: 写真は prev セッションに付け替える（session_id を更新する）。
    """
    prev = make_session(st, label="昼食", confirmed=1, started_at=jst(120))
    new = make_session(st, label="間食", confirmed=0, started_at=jst(30))
    photo = make_photo(st, new)

    run(st.lc.handle_merge_postback(f"merge:{new}:{prev}", SENDER_A))

    assert st.db.one("SELECT id FROM meal_photos WHERE id = ?", photo) is None, \
        "統合で写真の行が消えた"


def test_mergeの同一IDと存在しないIDは何も変えない(st):
    """壊れた merge データの実測（良い挙動の固定）。"""
    sid = make_session(st, confirmed=1)
    before = st.db.counts()

    assert "統合に失敗" in run(st.lc.handle_merge_postback(f"merge:{sid}:{sid}", SENDER_A))
    assert "統合に失敗" in run(st.lc.handle_merge_postback(f"merge:{sid}:99999", SENDER_A))
    assert "統合に失敗" in run(st.lc.handle_merge_postback(f"merge:99999:{sid}", SENDER_A))

    assert st.db.counts() == before


# ============================================================
# 6. 外部キーと人物IDの直書き
# ============================================================
# LINE のボタンが渡す person_id には2系統ある。
#   attribute:<sid>:<person_id>  … persons から生成した動的な id（存在チェックあり）
#   sess_confirm:<sid>:1|2|3     … 「祖母さん/母さん/祖父さん」= **1/2/3 の直書き**
#   bath_cls:<id>:grandma|mother|grandpa … 同じく 1/2/3 の直書き
# 直書き側は「既定シードの並び（1=祖母, 2=母, 3=祖父）」を前提にしている。
def test_sess_confirmは人物の存在を確認せずFK違反で失敗する(st):
    """`sess_confirm` が person_id を検証しないことの実測。

    「母さん」ボタンは person_id=2 を直書きする。その行が無い DB では
    `meal_sessions.person_id REFERENCES persons(id)` に当たって IntegrityError に
    なり、家族には「⚠️ 処理に失敗しました」だけが返る（何が悪いか分からない）。
    しかも直前の「セッション情報取得」では person が無いことを検知していて
    person_name を "?" にしているのに、そのまま UPDATE へ進んでいる。

    **実機の persons には id=2 が存在しない**（2026-10-15 時点で id は
    0,1,3,4,5,6,7,8。読み取り専用で確認）。つまりこのボタンは本番で
    必ず失敗する。「反応するときとしないときがある」の一因になりうる。

    本来: 人物が無ければその場で「その人物は登録されていません」と返す。
    `attribute` は persons から Quick Reply を生成し、同じ検証も持っている。
    """
    st.db.w("DELETE FROM persons WHERE id = 2")   # 実機と同じ「id=2 が無い」状態にする
    sid = make_session(st, person_id=0, label="昼食", confirmed=0)

    reply = run(st.lc.handle_session_confirm_postback(f"sess_confirm:{sid}:2", SENDER_A))

    assert reply == "⚠️ 処理に失敗しました"
    row = st.db.one("SELECT person_id, confirmed FROM meal_sessions WHERE id = ?", sid)
    assert (row["person_id"], row["confirmed"]) == (0, 0), "DB は変わらない（FK が守った）"
    assert st.broadcasts == []


def test_bath_clsはFKが無いので存在しない人物IDを黙って保存する(st):
    """`bath_classifications.confirmed_person_id` に FK が無いことの実測。

    「母さん」= person_id=2 を直書きするが、この列は FK も検証も無いので
    存在しない人物 ID がそのまま入り、家族には成功と返る。
    学習データ（誰の入浴か）が存在しない人物に紐づく。

    `other` を選んだ場合は person_id=None → **-1** が入る。
    スキーマのコメントは「NULL=未回答、0=誰もいない、>0=人物ID」で -1 は未定義。
    `confirmed_person_id is not None` を「回答済み」の判定に使っているため
    動いてはいるが、人物として解釈する側からは意味が取れない値。

    **実機には id=2 が無いまま、confirmed_person_id=2 の行が 17 件ある**
    （読み取り専用で確認）。「母さん」ボタンの回答は全部宛先不明の学習データに
    なっている。

    本来: 人物の存在を確認し、「他の家族」は 0 か専用の kind で表す。
    """
    st.db.w("DELETE FROM persons WHERE id = 2")
    rec_mother = st.db.w(
        "INSERT INTO bath_classifications(detected_at, hour_of_day) VALUES(?, 19)", jst(60))
    rec_other = st.db.w(
        "INSERT INTO bath_classifications(detected_at, hour_of_day) VALUES(?, 20)", jst(30))

    assert "母さんの入浴として記録しました" in run(
        st.lc.handle_bath_classification_postback(f"bath_cls:{rec_mother}:mother", SENDER_A))
    assert "他の家族の入浴として記録しました" in run(
        st.lc.handle_bath_classification_postback(f"bath_cls:{rec_other}:other", SENDER_A))

    assert st.db.scalar(
        "SELECT confirmed_person_id FROM bath_classifications WHERE id = ?", rec_mother) == 2
    assert st.db.one("SELECT id FROM persons WHERE id = 2") is None, "参照先は存在しない"
    assert st.db.scalar(
        "SELECT confirmed_person_id FROM bath_classifications WHERE id = ?", rec_other) == -1


def test_attributeは存在しない人物を拒否する(st):
    """存在チェックがある側の確認（良い挙動の固定）。"""
    sid = make_session(st, person_id=0, confirmed=0)

    reply = run(st.lc.handle_attribute_postback(f"attribute:{sid}:999", SENDER_A))

    assert "登録されていません" in reply
    assert st.db.scalar("SELECT person_id FROM meal_sessions WHERE id = ?", sid) == 0
    assert run(st.lc.handle_attribute_postback("attribute:99999:1", SENDER_A)) \
        == "⚠️ 対象の記録が見つかりません"


def test_rice_classificationsはイベント削除後も残る(st):
    """学習データだけが残って参照先が消える実測（コメント通りの意図的な設計）。

    `rice_classifications.event_id` には FK が無いため、保温と答えて
    イベントが消えても学習行は残る。後から「この分類はどのイベントだったのか」
    を辿ることはできない。
    """
    eid = make_event(st, value=55.0, started_at=jst(20))

    run(st.lc.handle_rice_action_postback(f"rice_action:{eid}:keep_warm", SENDER_A))

    cls = st.db.one("SELECT event_id, power_w, classification FROM rice_classifications")
    assert cls["event_id"] == eid and cls["power_w"] == 55.0
    assert st.db.one("SELECT id FROM events WHERE id = ?", eid) is None


# ============================================================
# 7. 同時実行（同じ通知に2人が答える）
# ============================================================
class _MaterializedCursor:
    """SELECT を読み切って閉じた状態を模すカーソル。"""

    def __init__(self, rows, lastrowid, rowcount):
        self._rows = list(rows)
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _inject_after_select(monkeypatch, db_module, marker: str, competing_sql: str, params: tuple):
    """production の SELECT 直後に「別の家族のリクエストが commit した」状態を挟む。

    `mark_notification_completed` は
        with transaction() as conn:
            row = conn.execute("SELECT id, completed_at FROM pending_notifications ...").fetchone()
            if row["completed_at"]: return False
            conn.execute("UPDATE pending_notifications ... WHERE id = ?")
    という read-then-write で、UPDATE 側に `completed_at IS NULL` が無い。
    `conn.execute(...).fetchone()` は一時カーソルを即破棄するので読み取りロックも
    残らず、2リクエストが同時に来ると両方が「未完了」を読む窓が実在する。
    その窓を決定論的に再現するために、SELECT と UPDATE の間に別コネクションの
    commit を割り込ませる（= もう1人の家族のリクエストが先に完了した状態）。
    """
    real_get_conn = db_module.get_conn
    fired: list[int] = []

    class Proxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            cur = self._conn.execute(sql, *args, **kwargs)
            rows = cur.fetchall() if sql.lstrip().upper().startswith("SELECT") else []
            lastrowid, rowcount = cur.lastrowid, cur.rowcount
            cur.close()
            if marker in sql and not fired:
                fired.append(1)
                other = sqlite3.connect(db_module.DB_PATH, timeout=5)
                try:
                    other.execute(competing_sql, params)
                    other.commit()
                finally:
                    other.close()
            return _MaterializedCursor(rows, lastrowid, rowcount)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(db_module, "get_conn",
                        lambda *a, **k: Proxy(real_get_conn(*a, **k)))
    return fired


def test_同時確認で先勝ちガードが効かず対応者が上書きされる(st, monkeypatch):
    """「既に他の家族が対応済みなら何もしない（先勝ちで競合ガード）」が成立しない実測。

    家族Bの確認が commit された直後に家族Aの UPDATE が走ると、
    `WHERE id = ?` だけなので completed_by / completed_action が A のものに
    上書きされ、A にも「確認を記録しました。家族全員に共有しました」が返って
    「☑️ Aさんが対応しました」が全家族に broadcast される。
    B の broadcast も既に出ているので、家族の LINE には同じ通知への
    「対応しました」が2通並び、DB に残る対応者は後から押した方になる。

    本来: UPDATE に `AND completed_at IS NULL` を付けて `rowcount` で勝者を
    判定する（1本の原子的な compare-and-swap にする）。
    直したら A 側が「既に他の家族が対応済みです」を返すことを assert する。
    """
    pid = make_pending(st, "bath_emergency", "2026-10-15_2200")
    _inject_after_select(
        monkeypatch, st.db_module,
        "SELECT id, completed_at FROM pending_notifications",
        "UPDATE pending_notifications SET completed_at = CURRENT_TIMESTAMP,"
        " completed_by = ?, completed_action = ? WHERE id = ?",
        (SENDER_B, "家族Bが先に確認", pid),
    )

    reply = run(st.lc.handle_confirm_postback("confirm:bath_emergency:2026-10-15_2200", SENDER_A))

    assert "確認を記録しました" in reply, "2人目なのに成功と返る"
    row = st.db.one("SELECT completed_by, completed_action FROM pending_notifications WHERE id = ?", pid)
    assert row["completed_by"] == SENDER_A, "先に対応した家族Bの記録が上書きされた"
    assert row["completed_action"] == "「浴室緊急通知」を確認"
    assert len(st.broadcasts) == 1, "A の broadcast も出る（B の分と合わせて2通になる）"


def test_sess_confirmは2人目の回答で黙って上書きされブロードキャストも出ない(st):
    """同じ食事の確認に2人が別の答えをしたときの実測（逐次でも起きる）。

    `handle_session_confirm_postback` は
      1. meal_sessions を UPDATE する
      2. そのあとで mark_notification_completed を呼ぶ
    の順。2人目の時点で通知は完了済みなので 2 は False を返して broadcast を
    出さないが、**1 の DB 書き込みは既に終わっている**。しかも戻り値は
    無条件に「✅ 記録しました」。
    結果、家族Aが「祖母の昼食」と答えた記録が、家族Bの「食事じゃない」で
    confirmed=-1 に落ち、他の家族には何も知らされない。
    祖母の食事回数が1回減り、誰も気づけない。

    本来: 1 の前に完了済みかを見て弾く（bath_cls は正しくそうしている）。
    """
    sid = make_session(st, person_id=0, label="昼食", confirmed=0, started_at=jst(45))
    make_pending(st, "session_confirm", f"session_{sid}")

    reply_a = run(st.lc.handle_session_confirm_postback(f"sess_confirm:{sid}:1", SENDER_A))
    assert "祖母" in reply_a
    assert len(st.sessions.sessions_today(GRANDMA_ID)) == 1
    broadcasts_after_a = len(st.broadcasts)

    reply_b = run(st.lc.handle_session_confirm_postback(f"sess_confirm:{sid}:reject", SENDER_B))

    assert "誤検知として記録しました" in reply_b, "2人目にも成功と返る"
    assert st.db.scalar("SELECT confirmed FROM meal_sessions WHERE id = ?", sid) == -1
    assert st.db.scalar("SELECT confirmed_by FROM meal_sessions WHERE id = ?", sid) == SENDER_B
    assert st.sessions.sessions_today(GRANDMA_ID) == [], "祖母の食事が1回消えた"
    assert len(st.broadcasts) == broadcasts_after_a, \
        "2人目の変更は誰にも通知されない"
    assert st.db.scalar(
        "SELECT completed_action FROM pending_notifications WHERE notification_type = 'session_confirm'"
    ) == "昼食を「テスト祖母さん」の食事として記録", "通知の記録は1人目のまま"


def test_attributeも2人目の回答で黙って上書きされる(st):
    """`attribute:` も同じ順序の問題を持つ（人物が黙って変わる）。"""
    eid = make_event(st, person_id=0)
    sid = make_session(st, person_id=0, confirmed=0, event_ids=[eid])
    make_pending(st, "attribute_session", f"session_{sid}")

    run(st.lc.handle_attribute_postback(f"attribute:{sid}:1", SENDER_A))
    broadcasts_after_a = len(st.broadcasts)
    reply_b = run(st.lc.handle_attribute_postback(f"attribute:{sid}:3", SENDER_B))

    assert "テスト家族" in reply_b
    assert st.db.scalar("SELECT person_id FROM meal_sessions WHERE id = ?", sid) == 3
    assert st.db.scalar("SELECT person_id FROM events WHERE id = ?", eid) == 3
    assert len(st.broadcasts) == broadcasts_after_a, "2人目の変更は broadcast されない"


def test_bath_clsは2人目を正しく弾く(st):
    """同じ入浴に2人が答えたときに書き込みを拒否する実装の確認（良い挙動の固定）。

    `confirmed_person_id is not None` を UPDATE と同じトランザクション内で
    見てから書くので、2人目は DB を変えない。他のハンドラもこの形に
    揃えるべき、という比較対象として残す。
    """
    rec = st.db.w(
        "INSERT INTO bath_classifications(detected_at, hour_of_day) VALUES(?, 19)", jst(60))

    assert "祖母さんの入浴" in run(
        st.lc.handle_bath_classification_postback(f"bath_cls:{rec}:grandma", SENDER_A))
    reply_b = run(st.lc.handle_bath_classification_postback(f"bath_cls:{rec}:cleaning", SENDER_B))

    assert reply_b == "ℹ️ この入浴は既に他の家族が回答済みです"
    row = st.db.one(
        "SELECT confirmed_person_id, confirmed_kind, confirmed_by FROM bath_classifications WHERE id = ?",
        rec)
    assert (row["confirmed_person_id"], row["confirmed_kind"]) == (1, "bathing")
    assert row["confirmed_by"] == SENDER_A


# ============================================================
# 8. 実際の webhook 経路（署名検証 → 許可リスト → postback）
# ============================================================
FAMILY_PASSWORD = "test-family-password-not-real"


@pytest.fixture
def web(st, monkeypatch):
    """署名付きで /line/webhook を叩けるクライアントと、送った返信の記録。"""
    from fastapi.testclient import TestClient
    import src.web.app as app_module

    replies: list[str] = []
    monkeypatch.setattr(app_module, "_load_line_secret", lambda: LINE_SECRET)
    monkeypatch.setattr(app_module, "_load_line_allowed_senders", lambda: {SENDER_A})
    monkeypatch.setattr(
        app_module, "_load_family_password",
        lambda: hashlib.sha256(FAMILY_PASSWORD.encode()).hexdigest(),
    )
    monkeypatch.setattr(app_module.limiter, "enabled", False)
    monkeypatch.setattr(
        st.notifier, "reply_line_message",
        lambda token, message: (replies.append(message), True)[1],
    )
    with TestClient(app_module.app, client=("203.0.113.9", 50000),
                    follow_redirects=False) as client:
        yield types.SimpleNamespace(client=client, module=app_module, replies=replies)


def _post_webhook(web, events: list[dict]):
    body = json.dumps({"events": events}).encode("utf-8")
    sig = base64.b64encode(
        hmac.new(LINE_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")
    return web.client.post(
        "/line/webhook", content=body,
        headers={"x-line-signature": sig, "content-type": "application/json"},
    )


def _postback_event(data: str, sender: str = SENDER_A) -> dict:
    return {
        "type": "postback",
        "source": {"type": "user", "userId": sender},
        "replyToken": "test-reply-token-not-real",
        "postback": {"data": data},
    }


def test_webhook1通のpostbackで祖母のイベントとセッションが消える(st, web):
    """ここまでの削除が「実際の webhook 経由で」到達することの確認。

    署名が通り許可リストに載っていれば、HTTP 1回でイベント・セッション・
    食事写真の行が消える。LINE のボタンはトーク履歴に残り続けるので、
    この入口は常に開いている。
    """
    eid = make_event(st, source="bath_humidity", event_type="presence", started_at=jst(90))
    sid = make_session(st, label="お風呂", confirmed=1, started_at=jst(90), event_ids=[eid])
    photo = make_photo(st, sid)

    resp = _post_webhook(web, [_postback_event(f"rice_action:{eid}:keep_warm")])

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert st.db.one("SELECT id FROM events WHERE id = ?", eid) is None
    assert st.db.one("SELECT id FROM meal_sessions WHERE id = ?", sid) is None
    assert st.db.one("SELECT id FROM meal_photos WHERE id = ?", photo) is None
    assert web.replies and "保温" in web.replies[0]


def test_未登録送信者のpostbackはDBを一切変更しない(st, web):
    """許可リストの外からの postback が state に触れないことの確認。

    「登録」コマンドだけは未登録者に通すが、それは message イベントの話で、
    postback は text_for_check が空になるため必ず許可リスト判定で落ちる。
    """
    eid = make_event(st)
    sid = make_session(st, event_ids=[eid])
    before = st.db.counts()

    resp = _post_webhook(web, [_postback_event(f"rice_action:{eid}:keep_warm", STRANGER)])

    assert resp.status_code == 200
    assert st.db.counts() == before
    assert web.replies == []
    assert st.matter == []


def test_署名が合わないpostbackはDBを変更しない(st, web):
    """署名検証が state の前に立っていることの確認。"""
    eid = make_event(st)
    make_session(st, event_ids=[eid])
    before = st.db.counts()

    body = json.dumps({"events": [_postback_event(f"rice_action:{eid}:keep_warm")]}).encode()
    resp = web.client.post("/line/webhook", content=body,
                           headers={"x-line-signature": "bm90LWEtcmVhbC1zaWduYXR1cmU="})

    assert resp.status_code == 401
    assert st.db.counts() == before


# 壊れた / 未知の postback データ。**返信もせず DB も変えずに終わる**ものの一覧。
# 家族から見ると「ボタンを押したのに何も起きない」になる。
SILENTLY_IGNORED_POSTBACKS = [
    "rice_action:5",            # 要素数が足りない
    "rice_action:abc:cook",     # event_id が数字でない
    "attribute:1",
    "attribute:x:1",
    "attribute:1:1:1",          # 区切りが多い（split(":") が 3 要素でない）
    "merge:1",
    "merge:1:x",
    "confirm:onlyone",
    "confirm_dismiss:onlyone",
    "bath_cls:1:nobody",        # 未知の選択肢
    "bath_cls:x:grandma",
    "sess_confirm:1:9",         # 未知の choice
    "sess_confirm:1:REJECT",    # 大文字（reject とは別物扱い）
    "lock_confirm:ctx:maybe",   # yes/no 以外
    "unknown_action:1:2",       # どの prefix にも当たらない
    "",                         # data 空
]


@pytest.mark.parametrize("data", SILENTLY_IGNORED_POSTBACKS)
def test_壊れたpostbackは返信もDB変更も無く黙って捨てられる(st, web, data):
    """「どの入力が黙って落ちるか」の一覧。

    どのハンドラも形式が合わないと `None` を返し、webhook は
    `if reply:` で送信を省くので**何も返らない**。LINE 側にはエラーも出ない。
    古いボタン・将来の仕様変更・data の作り間違いはすべてここに落ちる。

    本来: 到達したが解釈できない postback は「この操作は古いため使えません」と
    返す（少なくともログに残して家族に無音にしない）。
    """
    make_session(st, confirmed=1)
    before = st.db.counts()

    resp = _post_webhook(web, [_postback_event(data)])

    assert resp.status_code == 200
    assert web.replies == [], f"{data!r} に返信が出た（想定外）"
    assert st.db.counts() == before, f"{data!r} で DB が変わった"
    assert st.matter == []


def test_家族ダッシュボードからも同じ破壊的操作に到達できる(st, web):
    """`POST /api/pending-notifications/{id}/respond` は postback data をそのまま渡す。

    家族UIのパスワードを知っていれば、LINE を経由せずに任意の
    `rice_action:<event_id>:keep_warm` を送ってイベントを消せる
    （notif_id はパスから取るが実際には使われず、body の data だけで動く）。
    対応者は `family_ui` 固定なので「誰が消したか」は残らない。
    """
    eid = make_event(st)
    sid = make_session(st, event_ids=[eid])
    notif = make_pending(st, "rice_action", f"event_{eid}")
    login = web.client.post("/family/login", data={"password": FAMILY_PASSWORD})
    assert login.status_code == 303, "テストのログイン手順が壊れている"

    resp = web.client.post(f"/api/pending-notifications/{notif}/respond",
                           json={"data": f"rice_action:{eid}:keep_warm"})

    assert resp.status_code == 200
    assert st.db.one("SELECT id FROM events WHERE id = ?", eid) is None
    assert st.db.one("SELECT id FROM meal_sessions WHERE id = ?", sid) is None
    assert st.db.scalar(
        "SELECT completed_by FROM pending_notifications WHERE id = ?", notif) == "family_ui"
