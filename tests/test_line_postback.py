"""LINE のボタン押下（postback）8ハンドラの動作検証。

起点の症状は「LINE のコマンドが反応するときとしないときがある」。
メッセージ側（「状況」「ヘルプ」等）は登録済みの送信者なら動くところまで確認済みなので、
このファイルは**ボタン側**を見る。家族から見た「押したのに無反応」は、ここでは
`handler が None を返す` → `app.py の if reply:` が偽 → **返信を1通も出さずに
HTTP 200 を返す**、という1経路で起きる。だから各ハンドラについて

  1. 正常系に何が DB に書かれて何が返るか
  2. 二度押し（LINE の webhook 再送・家族の連打）で二重に書かれないか
  3. 存在しない ID / 負数 / 巨大な数 / 数値でない文字列を渡したとき、
     webhook まで例外が飛ぶのか、黙って無反応になるのか
  4. `data` は家族の端末から来る任意文字列なので、他人のセッションを操作できるか
  5. 期限切れ（完了済み pending・削除済みセッション）のボタンを押したらどうなるか

を全部実測して固定している。LINE の Quick Reply ボタンは**トーク履歴に残り続けて
何日後でも押せる**ので、5 は想像上の話ではない。

**このファイルのテストが通ることは「正しい」の意味ではない。** 実装にバグがあるものは
characterization テスト（現状の固定）にして、docstring に「本来どうあるべきか」と
「直したらこう書き換える」を書いた。`# BUG:` で始まる docstring がそれ。

本番への副作用はゼロにしてある:
  - LINE の送信系は全部 `line_stub` で差し替え（家族のスマホに通知を飛ばさない）
  - `notifier.requests` と `_enqueue_outbox` は呼ばれたら落ちるガードに差し替え
    （本番の `data/line_outbox.jsonl` に積まない）
  - Matter の電源操作は `_matter_set_power` を差し替え（実機の炊飯器を触らない）
  - DB は conftest の `temp_db` / `seeded_db`

テストデータは全部架空。実在の個人名・LINE user ID は書かない。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import types
from datetime import datetime, timedelta

import pytest

# --- 架空のテスト用資格情報・ID。実物とは一切関係ない -------------------------
SENDER = "U" + "a" * 32          # 登録済みの「テスト家族」
OTHER_SENDER = "U" + "b" * 32    # もう1人の登録済み家族（先勝ち検証用）
STRANGER = "U" + "c" * 32        # 未登録の第三者
GROUP_ID = "C" + "d" * 32        # グループトーク
LINE_SECRET = "test-line-channel-secret-not-real"
FAMILY_PASSWORD = "test-family-password-not-real"
TABLET_TOKEN = "test-tablet-token-not-real"
SESSION_SECRET = "0" * 64
FAKE_ENV = {
    "LINE_CHANNEL_ACCESS_TOKEN": "test-token-not-real",
    "LINE_USER_ID": "U" + "9" * 32,
    "LINE_ALLOWED_SENDERS": f"{SENDER},{OTHER_SENDER}",
    "LINE_ADMIN_DISPLAY_NAME": "テスト開発者",
    "RICE_COOKER_NODE_ID": "1",
}


# ============================================================
# 足場
# ============================================================

@pytest.fixture
def line_stub(monkeypatch):
    """外界に出る副作用を全部止めて、代わりに記録する。

    ハンドラは `from .notifier import ...` を**関数の中で**やっているので、
    モジュール属性を差し替えれば呼び出し時に解決される。
    `mark_notification_completed` は本物を通す（pending の競合ガードが
    このファイルの検証対象そのものなので、ここをモックすると何も見えない）。
    その本物が内部で呼ぶ `broadcast_line_message` だけを差し替える。
    """
    import src.lock_manager as lock_manager
    import src.notifier as notifier

    rec = types.SimpleNamespace(broadcasts=[], pushes=[], replies=[], matter=[])

    monkeypatch.setattr(notifier, "_load_env", lambda: dict(FAKE_ENV))
    monkeypatch.setattr(
        notifier, "broadcast_line_message",
        lambda msg: (rec.broadcasts.append(msg), 1)[1],
    )
    monkeypatch.setattr(
        notifier, "send_line_message",
        lambda msg, user_id=None: (rec.pushes.append((user_id, msg)), True)[1],
    )
    monkeypatch.setattr(
        notifier, "broadcast_with_quick_reply",
        lambda msg, items: (rec.broadcasts.append(msg), 1)[1],
    )
    monkeypatch.setattr(
        notifier, "send_line_with_quick_reply",
        lambda msg, items, user_id=None: (rec.pushes.append((user_id, msg)), True)[1],
    )
    monkeypatch.setattr(
        notifier, "reply_line_message",
        lambda token, msg: (rec.replies.append(msg), True)[1],
    )

    def _no_http(*a, **k):
        raise AssertionError("テストが LINE API を叩こうとした")

    def _no_outbox(*a, **k):
        raise AssertionError("テストが本番の data/line_outbox.jsonl に積もうとした")

    monkeypatch.setattr(notifier, "requests", types.SimpleNamespace(post=_no_http))
    monkeypatch.setattr(notifier, "_enqueue_outbox", _no_outbox)

    async def _matter(node_id, on):
        rec.matter.append((node_id, on))
        return True

    monkeypatch.setattr(lock_manager, "_matter_set_power", _matter)
    monkeypatch.setenv("RICE_COOKER_NODE_ID", "1")
    return rec


@pytest.fixture
def db(seeded_db):
    """postback の対象行を作るためのミニヘルパ。

    `seeded_db` は persons だけなので、meal_sessions / events /
    pending_notifications / bath_classifications は各テストで作る。
    """
    path = seeded_db

    def conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    def run(sql, *args):
        c = conn()
        try:
            c.execute(sql, args)
            c.commit()
        finally:
            c.close()

    def rows(sql, *args):
        c = conn()
        try:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.close()

    def one(sql, *args):
        got = rows(sql, *args)
        return got[0] if got else None

    def session(sid, person_id=0, label="昼食",
               started="2026-10-15 12:00:00",
                ended=None, confirmed=0, event_count=1):
        run(
            """INSERT INTO meal_sessions
                   (id, person_id, started_at, ended_at, event_count, label, confirmed)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            sid, person_id, started, ended or started, event_count, label, confirmed,
        )

    def event(eid, source="rice_cooker", event_type="power_on",
              started="2026-10-15 12:00:00", value=700.0, person_id=None):
        run(
            """INSERT INTO events(id, person_id, source, event_type, started_at, value)
               VALUES(?, ?, ?, ?, ?, ?)""",
            eid, person_id, source, event_type, started, value,
        )

    def link(sid, eid):
        run("INSERT INTO session_events(session_id, event_id) VALUES(?, ?)", sid, eid)

    def photo(pid, sid, file_name="fake-test-photo.jpg"):
        run(
            """INSERT INTO meal_photos(id, session_id, person_id, file_name, taken_at)
               VALUES(?, ?, 1, ?, '2026-10-15 12:05:00')""",
            pid, sid, file_name,
        )

    def pending(ntype, ckey, completed=False, created_at=None):
        run(
            """INSERT INTO pending_notifications
                   (notification_type, context_key, message, quick_reply_json,
                    created_at, completed_at)
               VALUES(?, ?, 'テスト通知', '[]', COALESCE(?, CURRENT_TIMESTAMP), ?)""",
            ntype, ckey, created_at,
            "2026-10-15 00:00:00" if completed else None,
        )

    def bath(rid, confirmed_person_id=None):
        run(
            """INSERT INTO bath_classifications(id, hour_of_day, confirmed_person_id)
               VALUES(?, 20, ?)""",
            rid, confirmed_person_id,
        )

    return types.SimpleNamespace(
        path=path, run=run, rows=rows, one=one, session=session, event=event,
        link=link, photo=photo, pending=pending, bath=bath,
    )


def call(handler, data, sender=SENDER):
    """postback ハンドラを1回呼ぶ。pytest-asyncio は入れていないので asyncio.run。"""
    return asyncio.run(handler(data, sender))


def call_twice(handler, data, sender=SENDER, sender2=None):
    """同じ postback を2回。LINE の再送・家族の連打・2人同時対応の再現。"""
    async def _go():
        a = await handler(data, sender)
        b = await handler(data, sender2 or sender)
        return a, b
    return asyncio.run(_go())


def handlers():
    import src.line_commands as lc
    return lc


# ============================================================
# 1. 「黙って落ちる入力」の一覧
#
# このセクションが今回の本題。ここに挙げた data はすべて **None が返り、
# webhook は返信を1通も出さずに 200 を返す**。家族からは「押したのに無反応」。
# ============================================================

SILENT_INPUTS = [
    # 数値であるべき ID が数値でない → int() の ValueError を握りつぶして None
    "attribute:abc:3",
    "merge:abc:5",
    "rice_action:abc:cook",
    "bath_cls:abc:grandma",
    "sess_confirm:abc:1",
    # 要素数が 3 でない（split(":") の長さチェックに落ちる）
    "attribute:7",
    "attribute:7:3:4",
    "merge:6",
    "merge:6:5:7",
    "rice_action:31",
    "rice_action:31:cook:x",
    "bath_cls:4",
    "bath_cls:4:grandma:x",
    "sess_confirm:9",
    "sess_confirm:9:1:x",
    "lock_confirm:x",
    "lock_confirm:a:b:c",
    "confirm:onlyonepart",
    "confirm_dismiss:onlyonepart",
    # 選択肢が既知の集合に無い
    "bath_cls:4:nobody",
    "bath_cls:4:",
    "sess_confirm:9:4",
    "sess_confirm:9:",
    "lock_confirm:2026-10-15_1200_lockreq:maybe",
    # 空の ID
    "attribute::",
    "attribute:7:1e3",
]


def _dispatch_like_webhook(data, sender=SENDER):
    """app.py の postback 分岐と同じ順序でハンドラを選ぶ（本体は app.py にある）。"""
    lc = handlers()
    table = [
        ("attribute:", lc.handle_attribute_postback),
        ("merge:", lc.handle_merge_postback),
        ("confirm:", lc.handle_confirm_postback),
        ("confirm_dismiss:", lc.handle_confirm_dismiss_postback),
        ("rice_action:", lc.handle_rice_action_postback),
        ("bath_cls:", lc.handle_bath_classification_postback),
        ("lock_confirm:", lc.handle_lock_confirm_postback),
        ("sess_confirm:", lc.handle_session_confirm_postback),
    ]
    for prefix, fn in table:
        if data.startswith(prefix):
            return call(fn, data, sender)
    return None


@pytest.mark.parametrize("data", SILENT_INPUTS)
def test_形式が壊れたボタンデータは黙って無反応になる(db, line_stub, data):
    """# BUG: 無反応は家族から見て「壊れている」と区別できない。

    8ハンドラはどれも `data` の形が合わないと `return None` で終わる。
    app.py 側は `if reply:` なので、返信も出ず、ログにも「postback処理エラー」も
    出ない（例外ではないため）。家族には「ボタンを押したが何も起きない」としか見えず、
    開発者側にも痕跡が残らない。報告されている「反応するときとしないときがある」の
    説明になりうる経路。

    本来は、形が合わない data も webhook 側でまとめて
    「このボタンは古くなりました。最新の通知から操作してください」を返すべき。
    直したら `assert reply is None` を「⚠️ を含む返信が返る」に書き換える。
    """
    db.session(7)
    db.session(6, person_id=1)
    db.session(9, person_id=0, label="夕食")
    db.event(31)
    db.bath(4)
    db.pending("rice_action", "event_31")
    db.pending("lock_confirm", "2026-10-15_1200_lockreq")

    reply = _dispatch_like_webhook(data)

    assert reply is None, f"{data!r} は無反応ではなくなった（実装が変わったらこの表を更新する）"
    assert line_stub.broadcasts == [], f"{data!r} で家族に broadcast が飛んだ"
    assert line_stub.matter == [], f"{data!r} で炊飯器を操作した"


def test_巨大なIDは例外になってハンドラの外まで飛ぶ(db, line_stub):
    """SQLite の INTEGER に収まらない ID は OverflowError になる。

    ValueError だけを握っているので、`int()` は通って `execute()` で落ちる。
    webhook 側は `except Exception` で拾って「⚠️ 処理に失敗しました」を返すので
    家族には何か返る（無反応にはならない）。500 にもならない。
    """
    lc = handlers()
    db.session(7)
    with pytest.raises(OverflowError):
        call(lc.handle_attribute_postback, "attribute:99999999999999999999:3")
    assert line_stub.broadcasts == []


# ============================================================
# 2. webhook 経由（署名・許可リストを通した実際の入口）
# ============================================================

@pytest.fixture
def web(db, line_stub, tmp_path, monkeypatch):
    """postback だけを見るために外部依存を差し替えた app。

    組み立て方は tests/test_web_authz.py の `web` fixture に倣う。
    """
    import src.web.app as app_module

    monkeypatch.setattr(
        app_module, "_load_family_password",
        lambda: hashlib.sha256(FAMILY_PASSWORD.encode()).hexdigest(),
    )
    monkeypatch.setattr(app_module, "_load_tablet_token", lambda: TABLET_TOKEN)
    monkeypatch.setattr(app_module, "_load_line_secret", lambda: LINE_SECRET)
    monkeypatch.setattr(
        app_module, "_load_line_allowed_senders", lambda: {SENDER, OTHER_SENDER}
    )
    monkeypatch.setattr(app_module, "_session_secret", lambda: SESSION_SECRET)
    monkeypatch.setattr(app_module, "MEAL_PHOTOS_DIR", tmp_path)
    monkeypatch.setattr(app_module.limiter, "enabled", False)
    return app_module


@pytest.fixture
def client(web):
    from fastapi.testclient import TestClient

    with TestClient(web.app, client=("203.0.113.9", 50000),
                    follow_redirects=False) as c:
        yield c


def _postback_body(data, sender=SENDER, source=None):
    return json.dumps({
        "events": [{
            "type": "postback",
            "replyToken": "test-reply-token",
            "source": source or {"userId": sender},
            "postback": {"data": data},
        }]
    }).encode()


def _sig(body):
    return base64.b64encode(
        hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()


def _press(client, data, sender=SENDER, source=None):
    body = _postback_body(data, sender, source)
    return client.post("/line/webhook", content=body,
                       headers={"x-line-signature": _sig(body)})


def test_webhook_は返信が無いボタン押下も200を返して黙る(client, line_stub):
    """# BUG: 「受け取ったが何もしなかった」が家族にも開発者にも見えない。

    壊れた data を webhook に投げたときの**入口側**の挙動。HTTP 200、
    reply_line_message は 0 回。LINE 側から見ると正常受信なので再送もされない。
    """
    r = _press(client, "rice_action:abc:cook")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert line_stub.replies == [], "無反応のはずが返信が出た"


def test_webhook_は未知の接頭辞のボタンを黙って捨てる(client, line_stub):
    """分岐表に無い data はどのハンドラにも渡らず、返信も出ない。

    `feedback_start` 以外の非 `<prefix>:` 形式はここに落ちる。将来ボタンを増やして
    app.py 側の分岐を書き忘れると、同じく無反応になる（家族は押したと思っている）。
    """
    r = _press(client, "some_new_button_nobody_wired_up")
    assert r.status_code == 200
    assert line_stub.replies == []


def test_webhook_はハンドラの例外を200と警告返信に変える(client, line_stub):
    """巨大 ID の OverflowError は 500 にならず「⚠️ 処理に失敗しました」になる。

    LINE は 500 を返すと再送してくるので、200 に丸めるのは正しい。
    ただし家族に出るのは原因不明の警告だけ。
    """
    r = _press(client, "attribute:99999999999999999999:3")
    assert r.status_code == 200
    assert line_stub.replies == ["⚠️ 処理に失敗しました"]


def test_webhook_は未登録の送信者のボタン押下を黙って捨てる(client, line_stub, db):
    """許可リストに無い送信者は postback でも弾かれる（返信も出ない）。

    炊飯器のロックまで届くボタンなので、ここは弾いて正しい。
    ただし弾いたことは送信者にも伝わらない。
    """
    db.pending("lock_confirm", "2026-10-15_1200_lockreq")
    r = _press(client, "lock_confirm:2026-10-15_1200_lockreq:yes", sender=STRANGER)
    assert r.status_code == 200
    assert line_stub.replies == []
    assert line_stub.matter == [], "未登録の第三者が炊飯器をロックできた"


def test_グループトークからのボタン押下は送信者IDがグループIDになり捨てられる(client, line_stub, db):
    """# BUG: 家族がグループトークでボタンを押すと必ず無反応になる。

    `_extract_sender_id` は `groupId or roomId or userId` の順で、グループ内の
    イベントでは**押した人の userId ではなくグループ ID** が送信者になる。
    許可リストは U... の個人 ID なので一致せず、黙って捨てられる。
    同じ人が1:1トークで押すと動く。「反応するときとしないときがある」の
    説明になりうる経路（家族が通知をグループに転送して運用していれば常に無反応）。

    本来は group/room では `source.userId` を見て許可判定すべき。
    直したら「ボタンが効く」側のテストに書き換える。
    """
    db.pending("rice_action", "event_31")
    db.event(31)
    r = _press(client, "rice_action:31:cook",
               source={"type": "group", "groupId": GROUP_ID, "userId": SENDER})
    assert r.status_code == 200
    assert line_stub.replies == [], "グループからのボタンが通るようになった"
    assert db.one("SELECT COUNT(*) c FROM rice_classifications")["c"] == 0


def test_confirm_dismiss_は_confirm_ハンドラに吸われない(client, line_stub, db):
    """分岐が `confirm:` を先に見ているので、接頭辞の取り違えがないかを固定する。

    `confirm_dismiss:` の 8 文字目は `_` なので `startswith("confirm:")` には
    一致しない。ここが崩れると「対応不要（誤検知）」が「確認した」に化ける。
    """
    db.pending("long_toilet_stay", "2026-10-15_1")
    r = _press(client, "confirm_dismiss:long_toilet_stay:2026-10-15_1")
    assert r.status_code == 200
    assert line_stub.replies == ["✅ 誰かさん、誤検知として記録しました（再通知停止）"]
    row = db.one("SELECT completed_action FROM pending_notifications")
    assert "誤検知として閉じました" in row["completed_action"]


@pytest.mark.parametrize("mode", ["api_error", "exception"])
def test_返信の送信が失敗するとDBだけ書かれて家族には何も届かない(
        client, line_stub, db, monkeypatch, mode):
    """# BUG: Reply API の失敗にフォールバックが無い（= 間欠的に「無反応」になる）。

    `reply_line_message` は失敗しても False を返すだけで、push への切り替えも
    outbox への退避もしない（`send_line_message` / `send_line_with_quick_reply`
    には outbox があるのに、返信だけ素通し）。webhook 側も
    `except Exception: log.error` で飲んで HTTP 200 を返すので、LINE は再送しない。

    結果として「ボタンの処理は走って DB も pending も更新されたのに、
    家族には1通も届かない」が成立する。reply token は1回限り・有効期限つきなので、
    webhook の再送や処理遅延が重なったときに**間欠的に**起きる。
    報告されている「反応するときとしないときがある」の説明になりうる経路。

    本来は返信が失敗したら push（send_line_message）に落とすべき。
    直したら「push で届く」アサートに書き換える。
    """
    import src.notifier as notifier
    db.event(31, value=700.0, started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    db.pending("rice_action", "event_31")
    db.pending("bath_emergency", "2026-10-15", completed=True)

    if mode == "api_error":
        monkeypatch.setattr(notifier, "reply_line_message", lambda token, msg: False)
    else:
        def _boom(token, msg):
            raise ConnectionError("テスト: 網経断")
        monkeypatch.setattr(notifier, "reply_line_message", _boom)

    # (1) 処理は成立しているが、押した本人への回答は失われる
    r = _press(client, "rice_action:31:cook")
    assert r.status_code == 200
    assert line_stub.replies == [], "返信が届いた（stub の差し替えが効いていない）"
    assert db.one("SELECT COUNT(*) c FROM rice_classifications")["c"] == 1, \
        "DB は書かれているのに返信だけが消えている"
    assert db.one(
        "SELECT completed_at FROM pending_notifications WHERE context_key='event_31'"
    )["completed_at"]
    # このケースは broadcast が代わりに飛ぶので、押した本人にも
    # 「☑️ 誰かさんが対応しました」だけは届く（具体的な回答は届かない）
    assert len(line_stub.broadcasts) == 1

    # (2) broadcast が出ない経路（完了済み pending の確認）では、
    #     返信が唯一の通知経路なので文字どおり何も届かない
    line_stub.broadcasts.clear()
    r2 = _press(client, "confirm:bath_emergency:2026-10-15")
    assert r2.status_code == 200
    assert line_stub.replies == []
    assert line_stub.broadcasts == [], "完了済み pending では broadcast も出ない = 完全な無反応"


def test_webhook_のログにどのボタンが押されたかが残らない(client, line_stub, caplog):
    """# BUG: postback の `data` がログに出ないので、無反応を後から追えない。

    `RECV type=... sender=...` のログは、message のときだけ本文の先頭20文字を
    残すが、postback のときは `data` を残さない。返信が出なかったとき
    （ハンドラが None を返したとき）のログも無い。そのため
    「ボタンを押したのに無反応」という報告に対して、
    受け取れていないのか・data の形が合わなかったのか・返信に失敗したのかを
    ログから切り分けられない。ログを足したのは 2026-10-15 の
    `_setup_web_logging` で、目的はまさにこの切り分けだった。

    本来は postback でも `data` を（先頭 60 文字程度まで）残し、
    返信を出さずに終わったイベントは warning で1行残すべき。
    直したら「data がログに出ている」アサートに書き換える。
    """
    import logging

    with caplog.at_level(logging.INFO, logger="line_webhook"):
        _press(client, "rice_action:abc:cook")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "type=postback" in text, "受信ログ自体が消えた"
    assert "rice_action" not in text, "data がログに出るようになった（この characterization は不要）"
    assert line_stub.replies == []


def test_feedback_start_ボタンは返信される(client, line_stub):
    """メニューの「💬 意見・質問」だけは app.py 側で直接返信を組んでいる。"""
    r = _press(client, "feedback_start")
    assert r.status_code == 200
    assert len(line_stub.replies) == 1
    assert "意見" in line_stub.replies[0]


# ============================================================
# 3. attribute: 未確定セッションの人物割当
# ============================================================

def test_attribute_正常系はセッションと配下イベントのperson_idを書き換える(db, line_stub):
    lc = handlers()
    db.session(7, person_id=0, label="昼食")
    db.event(11, person_id=None)
    db.link(7, 11)
    db.pending("attribute_session", "session_7")

    reply = call(lc.handle_attribute_postback, "attribute:7:3")

    assert reply == "✅ 昼食を「テスト家族さん」のものとして記録しました"
    assert db.one("SELECT person_id FROM meal_sessions WHERE id=7")["person_id"] == 3
    assert db.one("SELECT person_id FROM events WHERE id=11")["person_id"] == 3
    assert db.one("SELECT completed_at FROM pending_notifications")["completed_at"]
    assert len(line_stub.broadcasts) == 1, "対応したことが家族全員に共有されていない"


def test_attribute_は人物を割り当てても_confirmed_を1にしない(db, line_stub):
    """# BUG: 人物を確定したのに「未確認」のまま残る。

    `sess_confirm` は `confirmed=1, confirmed_by=...` を書くのに、`attribute` は
    `person_id` だけ書いて `confirmed` を触らない。`sessions_today()` の既定は
    `confirmed=1` のみなので、家族がボタンで人物を答えた食事が
    状況サマリ・食事回数・炊飯器ロック判定のどれにも載らない。
    `recheck_pending` の自動確定も pending が完了済みなので走らない。

    本来は `confirmed=1, confirmed_by=<sender>, confirmed_at=...` も書くべき。
    直したら下の 2 行を `== 1` / `is not None` に書き換える。
    """
    lc = handlers()
    db.session(7, person_id=0, label="昼食")
    db.pending("attribute_session", "session_7")

    from src.sessions import sessions_today

    call(lc.handle_attribute_postback, "attribute:7:1")

    row = db.one("SELECT confirmed, confirmed_by FROM meal_sessions WHERE id=7")
    assert row["confirmed"] == 0, "confirmed を書くようになった（この characterization は不要）"
    assert row["confirmed_by"] is None
    # 下流への影響: 当日の確定済みセッションとして一切出てこない
    assert sessions_today(1) == [], "集計に載るようになった（この characterization は不要）"


def test_attribute_の二度押しはDBを二重に書かない(db, line_stub):
    """二度押しは冪等（同じ値で UPDATE）。broadcast は1回だけ。

    ただし2回目も「✅ 記録しました」が返るので、家族からは成功が2回見える。
    pending が完了済みのため `mark_notification_completed` は False を返すが、
    ハンドラはその戻り値を見ていない。
    """
    lc = handlers()
    db.session(7, person_id=0)
    db.pending("attribute_session", "session_7")

    first, second = call_twice(lc.handle_attribute_postback, "attribute:7:3")

    assert first == second
    assert first.startswith("✅")
    assert db.one("SELECT person_id FROM meal_sessions WHERE id=7")["person_id"] == 3
    assert len(line_stub.broadcasts) == 1, "二度押しで家族に2通飛んだ"


def test_attribute_は確定済みの他人の記録を無確認で上書きできる(db, line_stub):
    """# BUG: 「既に確定済みなら上書き確認」とコメントにあるが、実際は log.info だけ。

    トーク履歴に残った古いボタンを誰かが押すと、別の家族が確定済みの
    人物割当が黙って書き換わる。送信者と対象セッションの紐付け検証も無い
    （どの pending 由来のボタンかを問わない）ので、任意のセッション ID を
    指定した data を送れば誰の記録でも書き換えられる。

    本来は、確定済み（confirmed=1）で別人が入っているセッションは
    「すでに〇〇さんで確定しています。変更しますか？」の二段確認にすべき。
    直したら「上書きされない」アサートに書き換える。
    """
    lc = handlers()
    db.session(7, person_id=1, label="夕食", confirmed=1)
    db.run("UPDATE meal_sessions SET confirmed_by = ? WHERE id = 7", OTHER_SENDER)

    reply = call(lc.handle_attribute_postback, "attribute:7:3")

    assert reply.startswith("✅")
    row = db.one("SELECT person_id, confirmed, confirmed_by FROM meal_sessions WHERE id=7")
    assert row["person_id"] == 3, "上書きガードが入った（この characterization は不要）"
    assert row["confirmed"] == 1
    assert row["confirmed_by"] == OTHER_SENDER, "確定者だけ古い名前が残る"


def test_attribute_は存在しないIDに警告を返す(db, line_stub):
    """無反応ではなく返信があるので、家族は押した結果が分かる。"""
    lc = handlers()
    db.session(7)
    assert call(lc.handle_attribute_postback, "attribute:999:3") == "⚠️ 対象の記録が見つかりません"
    assert call(lc.handle_attribute_postback, "attribute:-1:3") == "⚠️ 対象の記録が見つかりません"
    assert call(lc.handle_attribute_postback, "attribute:7:999") == \
        "⚠️ person_id=999 は登録されていません。"
    assert line_stub.broadcasts == []


def test_attribute_で祖母に確定すると直近2食で炊飯器が自動ロックされる(frozen_now, db, line_stub):
    """人物割当の副作用でロックが走る経路。実機の Matter は差し替えてある。

    `person_id == 1`（祖母）のときだけ、当日の確定済み食事が2回以上 +
    直近90分以内なら炊飯器をロックする。try/except で囲まれているので、
    ここが壊れても家族には何も出ない（返信に 🔒 の行が消えるだけ）。
    """
    lc = handlers()
    now = datetime.now()
    db.session(1, person_id=1, label="朝食", confirmed=1,
               started=(now - timedelta(minutes=70)).strftime("%Y-%m-%d %H:%M:%S"))
    db.session(2, person_id=1, label="昼食", confirmed=1,
               started=(now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"))
    db.session(7, person_id=0, label="間食",
               started=now.strftime("%Y-%m-%d %H:%M:%S"))

    reply = call(lc.handle_attribute_postback, "attribute:7:1")

    assert "🔒" in reply, "直近食事ありなのにロックされなかった"
    assert line_stub.matter == [(1, False)], "Matter に電源OFFが行っていない"
    assert db.one("SELECT is_locked FROM device_state WHERE device_name='rice_cooker'")["is_locked"] == 1


# ============================================================
# 4. merge: 「前と同じ食事」
# ============================================================

def _merge_fixture(db):
    db.session(5, person_id=1, label="昼食",
               started="2026-10-15 12:00:00")
    db.session(6, person_id=0, label="間食",
               started="2026-10-15 12:40:00")
    db.event(21)
    db.event(22)
    db.link(5, 21)
    db.link(6, 22)
    db.pending("attribute_session", "session_6")


def test_merge_正常系は新セッションを前のセッションに吸収する(db, line_stub):
    lc = handlers()
    _merge_fixture(db)

    reply = call(lc.handle_merge_postback, "merge:6:5")

    assert reply.startswith("✅ 「前と同じ食事」として統合しました。")
    assert db.rows("SELECT id FROM meal_sessions") == [{"id": 5}], "新セッションが残っている"
    row = db.one("SELECT started_at, ended_at, event_count, person_id FROM meal_sessions WHERE id=5")
    assert row["started_at"] == "2026-10-15 12:00:00"
    assert row["ended_at"] == "2026-10-15 12:40:00"
    assert row["event_count"] == 2
    assert sorted(r["event_id"] for r in db.rows("SELECT event_id FROM session_events")) == [21, 22]
    assert {r["person_id"] for r in db.rows("SELECT person_id FROM events")} == {1}
    assert db.one("SELECT completed_at FROM pending_notifications")["completed_at"]


def test_merge_は統合した側に紐づく食事写真を道連れに削除する(db, line_stub):
    """# BUG: 「前と同じ食事」を押すと、その食事の写真が DB から消える。

    `merge_sessions_manual` は `session_events` は付け替えるが `meal_photos` は
    付け替えずに `DELETE FROM meal_sessions WHERE id = new` する。
    `meal_photos.session_id` は `ON DELETE CASCADE` で、`get_conn()` は
    `foreign_keys=ON` なので、**写真の行がカスケード削除される**。
    祖母がタブレットで撮った直後の食事ほど写真が付いているので、
    家族が「前と同じ食事」を押すたびに最新の写真が消える
    （ファイル自体は data/meal_photos/ に孤児として残る）。

    本来は `UPDATE meal_photos SET session_id = <prev> WHERE session_id = <new>` を
    イベント付け替えと同じトランザクションで行うべき。
    直したら「写真が prev に付け替わっている」アサートに書き換える。
    """
    lc = handlers()
    _merge_fixture(db)
    db.photo(1, 6)
    assert db.rows("SELECT id FROM meal_photos") == [{"id": 1}]

    call(lc.handle_merge_postback, "merge:6:5")

    assert db.rows("SELECT id, session_id FROM meal_photos") == [], \
        "写真が保全されるようになった（この characterization は不要）"


def test_merge_の二度押しは失敗を返す(db, line_stub):
    """2回目は新セッションが消えているので「見つかりません」。冪等。"""
    lc = handlers()
    _merge_fixture(db)

    first, second = call_twice(lc.handle_merge_postback, "merge:6:5")

    assert first.startswith("✅")
    assert second == "⚠️ 統合に失敗しました（対象の食事記録が見つかりません）"
    assert len(line_stub.broadcasts) == 1
    assert db.rows("SELECT id FROM meal_sessions") == [{"id": 5}]


def test_merge_は同一IDと存在しないIDを弾く(db, line_stub):
    lc = handlers()
    _merge_fixture(db)
    for data in ("merge:6:6", "merge:6:999", "merge:999:5", "merge:-1:-2"):
        assert call(lc.handle_merge_postback, data) == \
            "⚠️ 統合に失敗しました（対象の食事記録が見つかりません）", data
    assert len(db.rows("SELECT id FROM meal_sessions")) == 2
    assert line_stub.broadcasts == []


def test_merge_は日付をまたぐ任意の2セッションを統合できる(db, line_stub):
    """# BUG: 送信者と対象の紐付け検証も、日付・順序の検証も無い。

    トーク履歴に残った古い「前と同じ食事」ボタン、あるいは任意の data を
    送れば、別の日の食事同士を1本にまとめられる。結果のセッションは
    started_at=前日 / ended_at=当日 になり、`sessions_today()` の当日判定から
    落ちて食事回数が狂う。

    本来は prev/new が同一人物・同日・一定時間内であることを検証すべき。
    直したら「⚠️ が返る」アサートに書き換える。
    """
    lc = handlers()
    db.session(5, person_id=1, label="夕食",
               started="2026-10-14 19:00:00")
    db.session(6, person_id=1, label="朝食",
               started="2026-10-15 08:00:00")

    reply = call(lc.handle_merge_postback, "merge:6:5")

    assert reply.startswith("✅"), "日付跨ぎの統合が弾かれた（この characterization は不要）"
    row = db.one("SELECT started_at, ended_at FROM meal_sessions WHERE id=5")
    assert row["started_at"] == "2026-10-14 19:00:00"
    assert row["ended_at"] == "2026-10-15 08:00:00"


# ============================================================
# 5. confirm / confirm_dismiss: 汎用の「確認した」「対応不要」
# ============================================================

def test_confirm_正常系はpendingを完了にして全家族に共有する(db, line_stub):
    lc = handlers()
    db.pending("bath_emergency", "2026-10-15")

    reply = call(lc.handle_confirm_postback, "confirm:bath_emergency:2026-10-15")

    assert reply == "✅ 誰かさん、確認を記録しました。家族全員に共有しました。"
    assert db.one("SELECT completed_at, completed_action FROM pending_notifications")["completed_action"] \
        == "「浴室緊急通知」を確認"
    assert line_stub.broadcasts == ["☑️ 誰かさんが対応しました\n「浴室緊急通知」を確認"]


def test_confirm_の二度押しは先勝ちで2人目に対応済みを返す(db, line_stub):
    """緊急通知は全家族に飛ぶので、2人が同時に押す状況が普通に起きる。"""
    lc = handlers()
    db.pending("bath_emergency", "2026-10-15")

    first, second = call_twice(
        lc.handle_confirm_postback, "confirm:bath_emergency:2026-10-15",
        sender2=OTHER_SENDER,
    )

    assert first.startswith("✅")
    assert second == "ℹ️ この通知は既に他の家族が対応済みです。"
    assert len(line_stub.broadcasts) == 1, "2人分の「対応しました」が家族全員に飛んだ"


def test_confirm_は存在しないcontext_keyでも成功を返して家族全員に通知する(db, line_stub):
    """# BUG: 閉じる対象が無いのに「確認を記録しました」になり broadcast まで出る。

    `mark_notification_completed` は pending 行が見つからないとき
    「古い通知かもしれない」として素通りし、broadcast して True を返す。
    そのため古いボタン（pending が `recheck_pending` で物理削除された、
    あるいは送信失敗で pending が作られなかったケース）や、任意の data を
    1通送るだけで、家族全員のスマホに「☑️ 誰かさんが対応しました」が届く。

    本来は pending が無いときは「この通知はもう有効ではありません」と
    送信者だけに返すべき（broadcast しない）。
    直したら「ℹ️ 返信・broadcast 0 件」に書き換える。
    """
    lc = handlers()

    reply = call(lc.handle_confirm_postback, "confirm:bath_emergency:9999-99-99")

    assert reply == "✅ 誰かさん、確認を記録しました。家族全員に共有しました。"
    assert len(line_stub.broadcasts) == 1, "pending 無しで broadcast されなくなった"
    assert db.rows("SELECT id FROM pending_notifications") == []


def test_confirm_は未知カテゴリの英語IDをそのまま家族のLINEに出す(db, line_stub):
    """`CATEGORY_LABELS` に無いカテゴリは生の識別子が本文に入る。

    過去に `long_toilet_stay` 等が未登録で40件流れた件の再発防止。
    現在は既知カテゴリは全部登録されているので、ここで見るのは
    「新しいカテゴリを足したときに英語が漏れる」構造が残っていること。
    """
    lc = handlers()
    db.pending("brand_new_category", "2026-10-15")

    call(lc.handle_confirm_postback, "confirm:brand_new_category:2026-10-15")

    assert "brand_new_category" in line_stub.broadcasts[0], \
        "未知カテゴリがラベル化されるようになった（この characterization は不要）"


def test_confirm_は食事アラートの確認で同日のロック通知も一括で閉じる(db, line_stub):
    """meal_alert と device_locked は同じ食事から派生するので片方で両方閉じる。

    前日分は残ること（`context_key LIKE '<当日>%'`）も一緒に固定する。
    ここが日付跨ぎで効くと、前日の未対応通知が黙って消える。
    """
    lc = handlers()
    db.pending("meal_alert", "2026-10-15_3")
    db.pending("device_locked", "2026-10-15_1629_rice_cooker")
    db.pending("device_locked", "2026-10-14_1000_rice_cooker")
    db.pending("session_confirm", "session_9")

    call(lc.handle_confirm_postback, "confirm:meal_alert:2026-10-15_3")

    done = {
        r["context_key"]: r["completed_at"] is not None
        for r in db.rows("SELECT context_key, completed_at FROM pending_notifications")
    }
    assert done == {
        "2026-10-15_3": True,
        "2026-10-15_1629_rice_cooker": True,
        "2026-10-14_1000_rice_cooker": False,
        "session_9": False,
    }
    assert len(line_stub.broadcasts) == 1, "一括クローズ分も broadcast された"


def test_confirm_dismiss_正常系と二度押し(db, line_stub):
    lc = handlers()
    db.pending("long_toilet_stay", "2026-10-15_1")

    first, second = call_twice(
        lc.handle_confirm_dismiss_postback,
        "confirm_dismiss:long_toilet_stay:2026-10-15_1", sender2=OTHER_SENDER,
    )

    assert first == "✅ 誰かさん、誤検知として記録しました（再通知停止）"
    assert second == "ℹ️ この通知は既に他の家族が対応済みです"
    assert len(line_stub.broadcasts) == 1


def test_confirm_dismiss_は誤検知を学習データとして残さない(db, line_stub):
    """# BUG: docstring は「完了マーク + 学習用に記録」と書いてあるが記録しない。

    実際に書かれるのは `pending_notifications.completed_action` の文面だけで、
    「どのカテゴリがどれだけ誤検知だったか」を機械的に集計できる行は作られない。
    誤検知率を見て閾値を直す、という使い方ができない。

    本来は誤検知を学習テーブル（rice_classifications / bath_classifications の
    ような専用行）に残すべき。直したらそのテーブルを見るアサートに書き換える。
    """
    lc = handlers()
    db.pending("bath_abnormal_temp", "2026-10-15_1")

    call(lc.handle_confirm_dismiss_postback, "confirm_dismiss:bath_abnormal_temp:2026-10-15_1")

    assert db.one("SELECT COUNT(*) c FROM rice_classifications")["c"] == 0
    assert db.one("SELECT COUNT(*) c FROM bath_classifications")["c"] == 0
    assert db.one("SELECT completed_action FROM pending_notifications")["completed_action"] \
        == "「浴室の異常温度」を誤検知として閉じました"


def test_confirm_は空のカテゴリと空のcontext_keyでも成功する(db, line_stub):
    """`confirm::` は split(":", 2) で3要素になるので通ってしまう。

    家族の LINE には「☑️ 誰かさんが対応しました / 「」を確認」が届く。
    """
    lc = handlers()
    reply = call(lc.handle_confirm_postback, "confirm::")
    assert reply.startswith("✅")
    assert "「」を確認" in line_stub.broadcasts[0]


# ============================================================
# 6. rice_action: 炊飯器の曖昧な power_on の分類
# ============================================================

def _rice_fixture(db, hour_now=True):
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if hour_now \
        else "2026-10-15 12:00:00"
    db.event(31, value=700.0, started=started)
    db.session(8, person_id=1, label="昼食", started=started)
    db.link(8, 31)
    db.pending("rice_action", "event_31")


@pytest.mark.parametrize("action,label,event_kept", [
    ("cook", "炊飯", True),
    ("lid_meal", "開けてご飯食べた", True),
    ("unknown", "判定保留", True),
    ("keep_warm", "保温", False),
    ("lid_only", "蓋を開けただけ", False),
])
def test_rice_action_の5択は学習データを残し食事かどうかで_event_の扱いが変わる(
        db, line_stub, action, label, event_kept):
    """炊飯/食べた/不明は event を残し、保温/蓋だけは event を削除する。

    削除側は「食事ではない」ので、食事回数と炊飯器ロック判定から外すのが設計意図。
    """
    lc = handlers()
    _rice_fixture(db)

    reply = call(lc.handle_rice_action_postback, f"rice_action:31:{action}")

    assert label in reply
    cls = db.rows("SELECT event_id, classification, classified_by FROM rice_classifications")
    assert cls == [{"event_id": 31, "classification": action, "classified_by": SENDER}]
    assert bool(db.rows("SELECT id FROM events WHERE id=31")) is event_kept
    assert db.one("SELECT completed_at FROM pending_notifications")["completed_at"]


def test_rice_action_の保温は紐づく食事セッションも消す(db, line_stub):
    """# BUG: 家族が確定済みの食事セッションでも、保温と答えると消える。

    保温/蓋だけのときは event を削除し、その event を失って空になった
    `meal_sessions` も削除する。セッションが `confirmed=1`（家族が「祖母の昼食」と
    答え済み）でも関係なく消えるので、家族が別経路で確定した食事記録が
    炊飯器の分類ボタン1つで消える。

    本来は confirmed=1 のセッションは消さず、「食事記録と矛盾します」と
    返して人間に判断させるべき。直したら「セッションが残る」アサートに書き換える。
    """
    lc = handlers()
    _rice_fixture(db)
    db.run("UPDATE meal_sessions SET confirmed = 1, confirmed_by = ? WHERE id = 8", OTHER_SENDER)

    call(lc.handle_rice_action_postback, "rice_action:31:keep_warm")

    assert db.rows("SELECT id FROM meal_sessions") == [], \
        "確定済みセッションが保護されるようになった（この characterization は不要）"
    assert db.rows("SELECT * FROM session_events") == []


def test_rice_action_の二度押しは学習データを二重に積む(db, line_stub):
    """# BUG: 同じ event に同じ分類が2行入る。

    `rice_classifications` に一意制約も「既に分類済みか」の確認も無いので、
    LINE の webhook 再送や家族の連打でそのまま2行目が入る。
    この表は「似た電力・時刻なら自動判定する」学習データで、件数が
    「📚 学習サンプル N件」の表示と自動判定の母数になるため、重複は
    特定の1イベントに重みを倍掛けすることになる。

    本来は `(event_id)` に一意制約を張るか UPSERT にすべき。
    直したら `len(cls) == 1` に書き換える。
    """
    lc = handlers()
    _rice_fixture(db)

    first, second = call_twice(lc.handle_rice_action_postback, "rice_action:31:cook")

    cls = db.rows("SELECT id, event_id, classification FROM rice_classifications")
    assert len(cls) == 2, "重複が防がれるようになった（この characterization は不要）"
    assert first == second, "2回目も成功と返る"
    assert len(line_stub.broadcasts) == 1


def test_rice_action_は存在しないevent_idでも成功を返し他の未回答通知まで閉じる(db, line_stub):
    """# BUG: 存在チェックが無く、かつ保温は 24 時間分の rice_action を一括で閉じる。

    実測した2つの問題:
      1. `event_id` が DB に無くても何も記録されないまま
         「✅ 保温として記録しました」が返り、家族全員に broadcast される
      2. 保温/蓋だけのとき、`notification_type='rice_action'` の未完了 pending を
         **24時間分まとめて** 完了にする。他の炊飯イベントについて誰も答えていない
         質問が、別のイベントの回答1つで黙って消える（家族UIの未対応一覧からも消える）

    1 と 2 が重なるので、存在しない event_id を1回送るだけで、その日の
    炊飯器の質問が全部閉じられる。送信者と対象の紐付け検証は無い。

    本来は event_id の存在確認を先に行い、無ければ
    「この通知は古くなりました」を返して何も閉じないべき。
    一括クローズも「同じ event から派生した pending だけ」に絞るべき。
    直したら「⚠️ が返り、他の pending は未完了のまま」に書き換える。
    """
    lc = handlers()
    _rice_fixture(db)
    db.pending("rice_action", "event_77")  # 誰も答えていない別の炊飯イベント

    reply = call(lc.handle_rice_action_postback, "rice_action:999999:keep_warm")

    assert reply.startswith("✅"), "存在チェックが入った（この characterization は不要）"
    assert "他の同種 2件の通知も同時に閉じました" in reply
    assert db.rows("SELECT COUNT(*) c FROM rice_classifications")[0]["c"] == 0, \
        "記録は1件も作られていないのに成功と返っている"
    closed = {
        r["context_key"]: r["completed_action"]
        for r in db.rows("SELECT context_key, completed_action FROM pending_notifications")
    }
    assert closed["event_77"] == "同日の保温分類により自動完了（保温）"
    assert db.rows("SELECT id FROM events WHERE id=31") == [{"id": 31}], \
        "存在しない ID の指定で実在の event が消えてはいけない"


def test_rice_action_は未知のaction文字列を判定保留として飲み込む(db, line_stub):
    """# BUG: CHECK 制約違反を握りつぶして「判定保留」として完了させる。

    `classification` の CHECK は 5 値だけ許すので、任意文字列の INSERT は失敗する。
    その例外は `log.warning` で飲まれ、分岐も `else`（unknown 相当）に落ちるため、
    **学習データは1行も残らないのに「判定保留にしました」と返って pending は閉じる**。
    家族には成功に見え、データとしては何も残らない。

    本来は既知の 5 値以外は `return None` ではなく
    「不正な操作です」を返し、pending も閉じないべき。
    直したら「⚠️ が返り pending が未完了」に書き換える。
    """
    lc = handlers()
    _rice_fixture(db)

    reply = call(lc.handle_rice_action_postback, "rice_action:31:totally-bogus")

    assert "判定保留" in reply, "未知の action が弾かれるようになった（この characterization は不要）"
    assert db.one("SELECT COUNT(*) c FROM rice_classifications")["c"] == 0
    assert db.one("SELECT completed_at FROM pending_notifications")["completed_at"]


def test_rice_action_は電力がNULLのeventで学習データを作らずに成功を返す(db, line_stub):
    """# BUG: 記録できなかったことが家族にも開発者にも伝わらない。

    `power_w` か `hour_of_day` が取れないと学習 INSERT 自体をスキップする。
    返信は通常どおり「✅ 記録しました」。蓋センサー故障などで value が
    入らない期間、家族はボタンを押し続けているのに学習データが増えない。

    本来は、学習に残せなかったときは文面を変える（または記録を必須にする）べき。
    直したら「学習データが1件入る」アサートに書き換える。
    """
    lc = handlers()
    db.run(
        """INSERT INTO events(id, source, event_type, started_at, value)
           VALUES(32, 'rice_cooker', 'power_on', '2026-10-15 12:00:00', NULL)"""
    )
    db.pending("rice_action", "event_32")

    reply = call(lc.handle_rice_action_postback, "rice_action:32:cook")

    assert reply.startswith("✅")
    assert db.one("SELECT COUNT(*) c FROM rice_classifications")["c"] == 0, \
        "NULL 電力でも記録されるようになった（この characterization は不要）"


# ============================================================
# 7. bath_cls: 浴室の湿度上昇が誰だったか
# ============================================================

@pytest.mark.parametrize("choice,person_id,kind,label", [
    ("grandma", 1, "bathing", "祖母さんの入浴"),
    ("grandpa", 3, "bathing", "祖父さんの入浴"),
    ("mother", 2, "bathing", "母さんの入浴"),
    ("yu_filling", 0, "yu_filling", "湯はり（人なし）"),
    ("cleaning", 0, "cleaning", "清掃"),
    ("no_one", 0, "unknown", "誰もいない"),
])
def test_bath_cls_の各選択肢が期待どおりの人物と種別を書く(
        db, line_stub, choice, person_id, kind, label):
    """ボタンのラベルと DB の値の対応を固定する。

    `person_id` は line_commands 側に**ハードコード**されている
    （grandma=1 / mother=2 / grandpa=3）。persons テーブルの実 ID と
    ずれたら全部の入浴学習データが別人として入る。
    `bath_classifications` には FK が無いので、ずれても例外にならない。
    """
    lc = handlers()
    db.bath(4)
    db.pending("bath_classification", "bath_4")

    reply = call(lc.handle_bath_classification_postback, f"bath_cls:4:{choice}")

    assert reply == f"✅ 誰かさん、{label}として記録しました"
    row = db.one(
        """SELECT confirmed_person_id, confirmed_kind, confirmation_method, confirmed_by
             FROM bath_classifications WHERE id = 4"""
    )
    assert row["confirmed_person_id"] == person_id
    assert row["confirmed_kind"] == kind
    assert row["confirmation_method"] == "line_reply"
    assert row["confirmed_by"] == SENDER
    assert db.one("SELECT completed_at FROM pending_notifications")["completed_at"]


def test_bath_cls_の_他_はスキーマに無い_マイナス1_を書く(db, line_stub):
    """# BUG: `confirmed_person_id = -1` はスキーマのコメントに無い値。

    スキーマは「NULL=未回答、0=誰もいない、>0=人物ID」と定義しているが、
    「他」を選ぶと -1 が入る（`person_id is None` を -1 に変換している）。
    `confirmed_person_id > 0` で絞る集計からは外れ、
    `IS NOT NULL` で絞る集計には入る、という曖味な値になる。

    本来は「他の家族」用のセンチネル（persons の id=0 と揃える、または
    confirmed_kind だけで表現する）を決めてスキーマに書くべき。
    直したら期待値をその値に書き換える。
    """
    lc = handlers()
    db.bath(4)

    call(lc.handle_bath_classification_postback, "bath_cls:4:other")

    assert db.one("SELECT confirmed_person_id FROM bath_classifications WHERE id=4")[
        "confirmed_person_id"] == -1


def test_bath_cls_は先に回答した家族の答えを守る(db, line_stub):
    """8ハンドラの中で唯一、対象行レベルで二度押しを明示的に防いでいる。

    2人目は DB を書けず「既に回答済み」が返る。他のハンドラもこの形にすべき。
    """
    lc = handlers()
    db.bath(4)
    db.pending("bath_classification", "bath_4")

    async def _go():
        a = await lc.handle_bath_classification_postback("bath_cls:4:grandma", SENDER)
        b = await lc.handle_bath_classification_postback("bath_cls:4:cleaning", OTHER_SENDER)
        return a, b

    first, second = asyncio.run(_go())

    assert first == "✅ 誰かさん、祖母さんの入浴として記録しました"
    assert second == "ℹ️ この入浴は既に他の家族が回答済みです"
    row = db.one("SELECT confirmed_person_id, confirmed_kind, confirmed_by FROM bath_classifications WHERE id=4")
    assert (row["confirmed_person_id"], row["confirmed_kind"]) == (1, "bathing")
    assert row["confirmed_by"] == SENDER
    assert len(line_stub.broadcasts) == 1


def test_bath_cls_は存在しない記録に警告を返す(db, line_stub):
    lc = handlers()
    db.bath(4)
    db.pending("bath_classification", "bath_4")

    assert call(lc.handle_bath_classification_postback, "bath_cls:999:grandma") == \
        "⚠️ 該当の入浴記録が見つかりません"
    assert call(lc.handle_bath_classification_postback, "bath_cls:-1:grandma") == \
        "⚠️ 該当の入浴記録が見つかりません"
    assert db.one("SELECT confirmed_person_id FROM bath_classifications WHERE id=4")[
        "confirmed_person_id"] is None
    assert db.one("SELECT completed_at FROM pending_notifications")["completed_at"] is None
    assert line_stub.broadcasts == []


# ============================================================
# 8. lock_confirm: 炊飯器をロックしてよいかの確認
# ============================================================

LOCK_CTX = "2026-10-15_1200_lockreq"


def test_lock_confirm_の_いいえ_はロックせず完了にする(db, line_stub):
    lc = handlers()
    db.pending("lock_confirm", LOCK_CTX)

    reply = call(lc.handle_lock_confirm_postback, f"lock_confirm:{LOCK_CTX}:no")

    assert reply == "✅ 誰かさん、ロックをスキップしました"
    assert line_stub.matter == [], "「ロックしない」で Matter を叩いた"
    assert db.one("SELECT completed_action FROM pending_notifications")["completed_action"] \
        == "誰かさんが「ロックしない」を選択 → スキップ"


def test_lock_confirm_の_はい_はMatterに電源OFFを出してdevice_stateを更新する(db, line_stub):
    """実機を触らないよう `_matter_set_power` を差し替えてある。

    node_id は `RICE_COOKER_NODE_ID` 環境変数（既定 1）から読む。
    `lock_manager.DEVICE_NODE_IDS` という対応表が別にあるのに、この経路だけ
    環境変数を直接読んでいる（値がずれたら別の機器を切る）。
    """
    lc = handlers()
    db.pending("lock_confirm", LOCK_CTX)

    reply = call(lc.handle_lock_confirm_postback, f"lock_confirm:{LOCK_CTX}:yes")

    assert reply == "✅ 誰かさん、炊飯器をロックしました"
    assert line_stub.matter == [(1, False)]
    row = db.one("SELECT is_locked, cycle_count_today, unlock_due_at FROM device_state WHERE device_name='rice_cooker'")
    assert row["is_locked"] == 1
    assert row["cycle_count_today"] == 1
    assert row["unlock_due_at"], "期限解除用の unlock_due_at が入っていない"


def test_lock_confirm_は期限も有効性も見ずに古いボタンでロックを実行する(db, line_stub):
    """# BUG: トーク履歴に残った古い「ロックする」が、何日後でも実機を切る。

    実測した2ケース（どちらも Matter に電源OFFが飛び、炊飯器が 120 分ロックされる）:
      1. その ctx の pending が存在しない（= 古い通知、または送信失敗で
         pending が作られなかったもの）
      2. pending が既に完了済み（他の家族が回答済み / 24時間で自動タイムアウト済み）

    2 の場合 `mark_notification_completed` は False を返すが、ハンドラは
    その戻り値を見ずに「✅ ロックしました」を返す。しかも broadcast も出ないので、
    **他の家族には炊飯器がロックされたことが伝わらない。**
    認知症の祖母から見ると、理由なく炊飯器が使えなくなる。

    本来は「対象 pending が未完了であること」を前提に、
    未完了でなければロックせず「この確認は期限切れです」を返すべき。
    直したら `matter == []` に書き換える。
    """
    lc = handlers()
    # 1. pending が存在しない古い ctx
    reply1 = call(lc.handle_lock_confirm_postback, "lock_confirm:2026-09-01_1200_lockreq:yes")
    assert reply1 == "✅ 誰かさん、炊飯器をロックしました", \
        "期限チェックが入った（この characterization は不要）"
    assert line_stub.matter == [(1, False)]

    # 2. 既に完了済みの pending
    db.pending("lock_confirm", LOCK_CTX, completed=True)
    broadcasts_before = len(line_stub.broadcasts)
    reply2 = call(lc.handle_lock_confirm_postback, f"lock_confirm:{LOCK_CTX}:yes")
    assert reply2 == "✅ 誰かさん、炊飯器をロックしました"
    assert line_stub.matter == [(1, False), (1, False)], "完了済みでもロックが走る"
    assert len(line_stub.broadcasts) == broadcasts_before, \
        "完了済みなので broadcast は出ない = 他の家族はロックを知らない"


def test_lock_confirm_の二度押しは二重にロックしてcycle_countを進める(db, line_stub):
    """# BUG: 冪等でない。連打すると `cycle_count_today` が押した回数だけ増える。

    `cycle_count_today` は1日のロック回数の上限判定に使う値。二度押しで
    水増しされると、本当に必要なロックが上限で止まる可能性がある。
    returned 値も2回とも「✅ ロックしました」。

    本来は「すでにロック済みなら Matter を叩かない」で済む
    （`get_device_state()` を見るだけ）。直したら `len(matter) == 1` に書き換える。
    """
    lc = handlers()
    db.pending("lock_confirm", LOCK_CTX)

    first, second = call_twice(lc.handle_lock_confirm_postback, f"lock_confirm:{LOCK_CTX}:yes")

    assert first == second == "✅ 誰かさん、炊飯器をロックしました"
    assert line_stub.matter == [(1, False), (1, False)], \
        "二度押しが防がれるようになった（この characterization は不要）"
    assert db.one("SELECT cycle_count_today FROM device_state WHERE device_name='rice_cooker'")[
        "cycle_count_today"] == 2


def test_lock_confirm_はMatter失敗時に承認済みとして閉じて警告を返す(db, line_stub, monkeypatch):
    """Matter 通信が落ちているときの経路。pending は「失敗」として完了になる。

    家族には ⚠️ が返るので無反応にはならない。ただし pending が閉じるため、
    ロックされていないのに再通知も家族UIの未対応一覧も出ない。
    """
    import src.lock_manager as lock_manager
    lc = handlers()
    db.pending("lock_confirm", LOCK_CTX)

    async def _fail(node_id, on):
        line_stub.matter.append((node_id, on, "FAIL"))
        return False

    monkeypatch.setattr(lock_manager, "_matter_set_power", _fail)

    reply = call(lc.handle_lock_confirm_postback, f"lock_confirm:{LOCK_CTX}:yes")

    assert reply == "⚠️ 誰かさん、承認は受けましたがロック実行に失敗しました（matter通信エラー）"
    assert db.one("SELECT completed_at, completed_action FROM pending_notifications")[
        "completed_action"] == "誰かさんが承認したがロック実行に失敗"
    assert db.one("SELECT is_locked FROM device_state WHERE device_name='rice_cooker'")[
        "is_locked"] == 0


def test_lock_confirm_はctxにコロンが入ると黙って無反応になる(db, line_stub):
    """# BUG: `split(":")` の要素数 3 固定なので、ctx の形式に依存して壊れる。

    現在の ctx は `%Y-%m-%d_%H%M_lockreq` でコロンを含まないため動いているが、
    時刻表記を `12:00` に変えた瞬間に全ロック確認ボタンが無反応になる。
    他の7ハンドラのうち `confirm` / `confirm_dismiss` は `split(":", 2)` で
    この問題を避けている。

    本来は `lock_confirm` も `split(":", 1)` + 末尾の yes/no 切り出しにすべき。
    直したら「ロックされる」アサートに書き換える。
    """
    lc = handlers()
    assert call(lc.handle_lock_confirm_postback, "lock_confirm:2026-10-15_12:00_lockreq:yes") is None
    assert line_stub.matter == [], "コロン入り ctx が通るようになった"


# ============================================================
# 9. sess_confirm: 新規食事セッションの「誰の食事ですか？」
# ============================================================

def _sess_fixture(db):
    db.session(9, person_id=0, label="夕食",
               started="2026-10-15 18:30:00")
    db.event(41, source="fridge", event_type="open")
    db.link(9, 41)
    db.pending("session_confirm", "session_9")


@pytest.mark.parametrize("choice,person_id,confirmed,fragment", [
    ("1", 1, 1, "テスト祖母さんの夕食として記録しました"),
    ("3", 3, 1, "テスト家族さんの夕食として記録しました"),
    ("other", 0, 1, "他の家族さんの夕食として記録しました"),
])
def test_sess_confirm_の正常系はperson_idとconfirmedを書く(
        db, line_stub, choice, person_id, confirmed, fragment):
    lc = handlers()
    _sess_fixture(db)

    reply = call(lc.handle_session_confirm_postback, f"sess_confirm:9:{choice}")

    assert reply == f"✅ 誰かさん、{fragment}"
    row = db.one("SELECT person_id, confirmed, confirmed_by FROM meal_sessions WHERE id=9")
    assert (row["person_id"], row["confirmed"]) == (person_id, confirmed)
    assert row["confirmed_by"] == SENDER
    assert db.one("SELECT completed_at FROM pending_notifications")["completed_at"]


def test_sess_confirm_の_食事じゃない_は却下として隠す(db, line_stub):
    """`confirmed=-1` にして UI から隠す。生イベントは残す（設計どおり）。"""
    lc = handlers()
    _sess_fixture(db)

    reply = call(lc.handle_session_confirm_postback, "sess_confirm:9:reject")

    assert reply == "✅ 誰かさん、誤検知として記録しました"
    assert db.one("SELECT confirmed FROM meal_sessions WHERE id=9")["confirmed"] == -1
    assert db.rows("SELECT id FROM events") == [{"id": 41}]


def test_sess_confirm_の選択肢はpersonsのid1と2と3に固定で依存している(db, line_stub):
    """選択肢の person_id が `monitor.py` と `line_commands.py` にハードコードされている。

    「祖母さん/母さん/祖父さん」ボタンはそれぞれ `sess_confirm:<sid>:1` / `:2` / `:3` を
    送り、ハンドラはその数字をそのまま `meal_sessions.person_id` に書く。
    `init_db()` の既定シードが 祖母=1 / 母=2 / 祖父=3 を作るので既定の DB では通る。
    ただし `meal_sessions.person_id` には FK があるので、persons を作り直して
    ID がずれた環境では FOREIGN KEY 違反になり、家族には原因の分からない
    「⚠️ 処理に失敗しました」しか返らない（ログにも `セッション確定失敗` の1行だけ）。

    `attribute:` 側は persons から動的にボタンを組んでいるので、
    `sess_confirm` もそれに揃えるべき。
    """
    lc = handlers()
    _sess_fixture(db)

    # 既定シードの id=2 があるので、母ボタンはそのまま通る
    name = db.one("SELECT name FROM persons WHERE id=2")["name"]
    assert call(lc.handle_session_confirm_postback, "sess_confirm:9:2") == \
        f"✅ 誰かさん、{name}さんの夕食として記録しました"
    assert db.one("SELECT person_id FROM meal_sessions WHERE id=9")["person_id"] == 2

    # id=2 が無い環境では同じボタンが FK 違反で潰れる
    db.run("UPDATE meal_sessions SET person_id = 0, confirmed = 0 WHERE id = 9")
    db.run("DELETE FROM persons WHERE id = 2")
    assert call(lc.handle_session_confirm_postback, "sess_confirm:9:2") == "⚠️ 処理に失敗しました"
    assert db.one("SELECT confirmed FROM meal_sessions WHERE id=9")["confirmed"] == 0


def test_sess_confirm_は存在しないセッションへの却下を成功として扱う(db, line_stub):
    """# BUG: reject 経路だけ存在チェックが無く、偽の成功が返る。

    1/2/3/other は先に `SELECT ... FROM meal_sessions` して無ければ
    「⚠️ 該当セッションが見つかりません」を返すのに、reject は
    いきなり UPDATE するだけ。0 行更新でもエラーにならないので、
    pending を閉じて家族全員に「食事カウントから除外」を broadcast する。
    古いボタンを押した家族には成功に見え、実際には何も起きていない。

    本来は reject も先に存在チェックすべき。
    直したら「⚠️ 該当セッションが見つかりません」に書き換える。
    """
    lc = handlers()
    _sess_fixture(db)

    reply = call(lc.handle_session_confirm_postback, "sess_confirm:999:reject")

    assert reply == "✅ 誰かさん、誤検知として記録しました", \
        "存在チェックが入った（この characterization は不要）"
    assert len(line_stub.broadcasts) == 1
    assert db.one("SELECT confirmed FROM meal_sessions WHERE id=9")["confirmed"] == 0

    # 比較: 却下以外は同じ状況で警告が返る
    assert call(lc.handle_session_confirm_postback, "sess_confirm:999:1") == \
        "⚠️ 該当セッションが見つかりません"


def test_sess_confirm_は後から押した家族が前の回答を黙って上書きする(db, line_stub):
    """# BUG: 先勝ちガードが無い（`bath_cls` にはある）。

    2人目の回答で person_id / confirmed_by が書き換わる。却下（-1）の後に
    「祖母」を押せば却下も取り消せる。どちらも2人とも「✅ 記録しました」が返るので、
    家族は自分の回答が最終だと思う。誰の回答が残ったかは家族には分からない
    （broadcast は1人目の分だけ）。

    本来は `confirmed != 0` のセッションは
    「すでに〇〇さんが回答済みです」で止めるべき。
    直したら「2人目は ℹ️ で DB は1人目のまま」に書き換える。
    """
    lc = handlers()
    _sess_fixture(db)

    async def _go():
        a = await lc.handle_session_confirm_postback("sess_confirm:9:reject", SENDER)
        b = await lc.handle_session_confirm_postback("sess_confirm:9:1", OTHER_SENDER)
        return a, b

    first, second = asyncio.run(_go())

    assert first.startswith("✅") and second.startswith("✅")
    row = db.one("SELECT person_id, confirmed, confirmed_by FROM meal_sessions WHERE id=9")
    assert (row["person_id"], row["confirmed"]) == (1, 1), \
        "先勝ちガードが入った（この characterization は不要）"
    assert row["confirmed_by"] == OTHER_SENDER
    assert len(line_stub.broadcasts) == 1, "2人目の分は broadcast されない"


def test_sess_confirm_のconfirmed_atはUTCで入る(db, line_stub):
    """started_at は JST、confirmed_at は UTC。混在を固定しておく。

    `CURRENT_TIMESTAMP` は SQLite の UTC。画面やレポートで
    `confirmed_at` を JST の時刻として表示すると9時間ずれる。
    """
    lc = handlers()
    _sess_fixture(db)

    call(lc.handle_session_confirm_postback, "sess_confirm:9:1")

    row = db.one("SELECT confirmed_at FROM meal_sessions WHERE id=9")
    now = db.one("SELECT datetime('now') AS utc, datetime('now', 'localtime') AS local")
    assert row["confirmed_at"][:16] == now["utc"][:16], "confirmed_at が UTC でなくなった"
    assert now["utc"] != now["local"], "テスト環境が UTC で、この比較が無意味になっている"
