"""`/line/webhook` の入口 — 届いてから `dispatch()` に渡るまでに何が落ちるか。

起点の症状は「LINE のコマンドが反応するときとしないときがある」。
応答速度・許可リストの中身・主要コマンドの疎通は別途切り分け済みなので、
ここでは**黙って捨てられる経路**だけを網羅的に固定する。

このファイルで判明して固定した、家族から見える振る舞い:

  - 冪等性の仕組みが**無い**。`webhookEventId` も `deliveryContext.isRedelivery` も
    読んでいないので、LINE が再送すると同じ操作が2回 DB に書かれる。
    返信は同じ `replyToken` を2回使うので、2回目は LINE 側で必ず失敗する。
  - `dispatch()` が例外を投げると webhook 全体が 500 になり、同じバッチの
    **後続イベントが処理されないまま消える**。postback 側だけ try/except がある。
  - 登録済みの家族が**グループ/トークルームで送ると無視される**。
    `_extract_sender_id` が groupId を userId より優先するので、
    許可リスト（個人の user ID が入っている）に当たらず「未許可送信者」で捨てられる。
  - 「メニュー」だけが reply ではなく **push** で送られるため、
    LINE 通知マスタースイッチ OFF のとき無反応になる（他のコマンドは返信する）。
  - 認識できない本文には**何の返信も無い**（「わかりません」も出ない）。
  - 返信が LINE に拒否されても/網が落ちても、再送も outbox 退避もされず、
    webhook は 200 を返すので LINE 側の再送も起きない。返信は永久に失われる。
  - 形の違うペイロード（`source: null` 等）で 500 になる箇所が多数ある。

**安全策（実稼働中の本番 Pi 上で走るため）**:
  - `requests.post` を差し替えて実際の LINE API を叩かせない。家族のスマホに通知は飛ばない
  - `notifier._enqueue_outbox` を差し替える。**これは `Path("data/line_outbox.jsonl")` と
    CWD 相対で開くので、差し替えないと本番の再送キューに書き込む**
  - `BASE` を一時ディレクトリに向けて本番 `.env` を読ませない
    （`_build_url_reply` は TABLET_TOKEN を返信本文に埋めるため）
  - `line_commands.unlock_device` を差し替えて Matter で炊飯器を触らせない

テストデータは全部架空。実在の個人名・LINE user ID・トークンは書かない。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

# --- 架空のテスト用資格情報。実物とは一切関係ない ---------------------------
LINE_SECRET = "test-line-channel-secret-not-real"
TABLET_TOKEN = "test-tablet-token-not-real"
PUBLIC_URL = "https://test-tunnel.example.invalid"

# LINE の ID は接頭辞と桁数だけ真似た架空値
REGISTERED = "U" + "a" * 32   # 許可リストに入っている「テスト家族」
STRANGER = "U" + "b" * 32     # 未登録の第三者
DB_ONLY = "U" + "c" * 32      # DB(family_line_users) にだけ登録されている家族
GROUP = "C" + "d" * 32        # グループ
ROOM = "R" + "e" * 32         # 複数人トークルーム

EXTERNAL_HOST = "203.0.113.9"  # ドキュメント用の外部 IP (RFC 5737)

REPLY_API = "https://api.line.me/v2/bot/message/reply"
PUSH_API = "https://api.line.me/v2/bot/message/push"

_UNSET = object()


# ============================================================
# 足場
# ============================================================

@pytest.fixture
def ingress(seeded_db, tmp_path, monkeypatch):
    """webhook の入口だけを見るために、外界に出る口を全部塞いだ app。"""
    import src.line_commands as line_commands
    import src.notifier as notifier
    import src.settings as settings
    import src.web.app as app_module

    real_allowed = app_module._load_line_allowed_senders

    # 本番 .env / tunnel_url.txt を読ませない。BASE.parent.parent/".env" が参照先。
    fake_root = tmp_path / "fakeroot"
    fake_base = fake_root / "src" / "web"
    fake_base.mkdir(parents=True)
    monkeypatch.setattr(app_module, "BASE", fake_base)
    monkeypatch.setattr(app_module, "_load_line_secret", lambda: LINE_SECRET)
    monkeypatch.setattr(app_module, "_load_line_allowed_senders", lambda: {REGISTERED})
    monkeypatch.setattr(app_module, "_load_tablet_token", lambda: TABLET_TOKEN)
    monkeypatch.setattr(app_module, "_current_tunnel_url", lambda: PUBLIC_URL)
    monkeypatch.setattr(app_module, "_session_secret", lambda: "0" * 64)
    monkeypatch.setattr(app_module.limiter, "enabled", False)
    monkeypatch.setattr(line_commands, "_load_public_base_url", lambda: PUBLIC_URL)

    # LINE API への唯一の出口を記録用に差し替える。
    # api dict でテスト側から応答（or 例外）を操作する。
    calls: list[dict] = []
    api = {"status": 200, "text": "{}", "error": None}

    def fake_post(url, **kwargs):
        calls.append({"url": url, "json": kwargs.get("json")})
        if api["error"] is not None:
            raise api["error"]
        return types.SimpleNamespace(status_code=api["status"], text=api["text"])

    # exceptions は本物を使う（notifier の except 節が型で分岐している）
    monkeypatch.setattr(
        notifier, "requests",
        types.SimpleNamespace(post=fake_post, put=fake_post,
                              exceptions=notifier.requests.exceptions),
    )
    monkeypatch.setattr(notifier, "_load_env",
                        lambda: {"LINE_CHANNEL_ACCESS_TOKEN": "test-access-token-not-real"})

    # **本番の再送キューに書かせない**（CWD 相対パスで開かれる）
    outbox: list[tuple] = []
    monkeypatch.setattr(notifier, "_enqueue_outbox",
                        lambda user_id, messages: outbox.append((user_id, messages)))

    # 炊飯器を実際に触らせない
    unlocks: list[str] = []

    async def fake_unlock(device_name, node_id, reason=""):
        unlocks.append(device_name)
        return True

    monkeypatch.setattr(line_commands, "unlock_device", fake_unlock)

    # 設定スイッチ。既定は現行デフォルトどおり ON。
    switches: dict[str, bool] = {}
    monkeypatch.setattr(settings, "get_bool",
                        lambda key, default=True: switches.get(key, default))

    # line_commands はロック解除コードをプロセス内 dict に持つ。テスト間で持ち越す。
    line_commands._pending_unlocks.clear()
    line_commands._lockouts.clear()
    yield types.SimpleNamespace(
        module=app_module, app=app_module.app, db=seeded_db,
        calls=calls, api=api, outbox=outbox, unlocks=unlocks,
        switches=switches, real_allowed=real_allowed, env_dir=fake_root,
    )
    line_commands._pending_unlocks.clear()
    line_commands._lockouts.clear()


def _sig(body: bytes, secret: str = LINE_SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")


def _post_raw(ingress, body: bytes, *, signature=_UNSET, allow_500: bool = False):
    """生のボディを投げる。signature=None でヘッダ無し、bytes/str でその値。

    allow_500=True のときだけハンドラ内の例外を 500 として観測する
    （TestClient の既定は例外をそのまま送出するので、落ちる経路が見えない）。
    """
    headers = {"content-type": "application/json"}
    if signature is _UNSET:
        headers["x-line-signature"] = _sig(body)
    elif signature is not None:
        headers["x-line-signature"] = signature
    client = TestClient(ingress.app, client=(EXTERNAL_HOST, 50000),
                        raise_server_exceptions=not allow_500)
    with client as c:
        return c.post("/line/webhook", content=body, headers=headers)


def _envelope(*events) -> bytes:
    return json.dumps({"destination": "U" + "f" * 32, "events": list(events)}).encode()


def _post(ingress, *events, allow_500: bool = False):
    return _post_raw(ingress, _envelope(*events), allow_500=allow_500)


def _text_event(text: str, *, sender: str = REGISTERED, token: str = "test-token-1",
                event_id: str = "TESTEVENT1", source=_UNSET, redelivery: bool = False) -> dict:
    return {
        "type": "message",
        "webhookEventId": event_id,
        "replyToken": token,
        "deliveryContext": {"isRedelivery": redelivery},
        "source": {"type": "user", "userId": sender} if source is _UNSET else source,
        "message": {"type": "text", "id": "testmsg1", "text": text},
    }


def _postback_event(data: str, *, sender: str = REGISTERED, token: str = "test-token-1",
                    event_id: str = "TESTPB1", source=_UNSET) -> dict:
    return {
        "type": "postback",
        "webhookEventId": event_id,
        "replyToken": token,
        "deliveryContext": {"isRedelivery": False},
        "source": {"type": "user", "userId": sender} if source is _UNSET else source,
        "postback": {"data": data},
    }


def _replies(ingress) -> list[str]:
    return [c["json"]["messages"][0]["text"]
            for c in ingress.calls if c["url"] == REPLY_API]


def _reply_tokens(ingress) -> list[str]:
    return [c["json"]["replyToken"] for c in ingress.calls if c["url"] == REPLY_API]


def _urls(ingress) -> list[str]:
    return [c["url"] for c in ingress.calls]


def _feedback_rows(ingress) -> int:
    """「意見 …」で1行増える。再送の重複を数えるのに使う（テーブルは遅延作成）。"""
    conn = sqlite3.connect(ingress.db)
    try:
        return conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _lock_rice_cooker(ingress) -> None:
    conn = sqlite3.connect(ingress.db)
    conn.execute(
        "INSERT OR REPLACE INTO device_state(device_name, is_locked) VALUES('rice_cooker', 1)"
    )
    conn.commit()
    conn.close()


def _register_in_db(ingress, line_user_id: str, person_id: int = 3) -> None:
    conn = sqlite3.connect(ingress.db)
    conn.execute(
        "INSERT OR REPLACE INTO family_line_users(line_user_id, person_id, display_name) "
        "VALUES(?, ?, 'テスト家族')",
        (line_user_id, person_id),
    )
    conn.commit()
    conn.close()


def _allowed_from_db(ingress):
    """許可リストを DB だけから作る版（.env 由来の固定許可を混ぜない）。"""
    def _inner():
        from src.db import get_conn
        conn = get_conn()
        try:
            return {r["line_user_id"]
                    for r in conn.execute("SELECT line_user_id FROM family_line_users")}
        finally:
            conn.close()
    return _inner


# ============================================================
# 1. 署名検証
# ============================================================

def test_署名検証はJSONパースより先に走る(ingress):
    """署名が無ければ、本文が壊れていても中身を見る前に 401 で切ること。

    順序が逆だと、署名を持たない誰でもパーサを突っつける。
    """
    resp = _post_raw(ingress, b"{not json", signature=None)
    assert resp.status_code == 401


def test_署名が正しくても本文が不正なJSONなら400(ingress):
    """400 は LINE から見ると失敗なので、同じ webhook が繰り返し再送される。

    LINE 自身が壊れた JSON を送ることは無いので実害は小さいが、
    「200 を返して再送を防ぐ」という方針（最後の `return {"ok": True}` のコメント）
    からは外れている。
    """
    resp = _post_raw(ingress, b"{not json")
    assert resp.status_code == 400


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"\x80" * 44], ids=["2byte", "44byte"])
def test_現状_署名ヘッダに非ASCIIバイトが来ると401ではなく500になる(ingress, raw):
    """`hmac.compare_digest` は非 ASCII の str を渡すと TypeError を投げる。

    HTTP ヘッダは latin-1 でデコードされるので、高位バイトを1つ入れるだけで
    **未認証の誰でも** webhook を 500 にできる（認証は突破できない）。
    実害はログ汚染と、障害調査時の見分けづらさ。

    本来どうあるべきか: 署名ヘッダは ASCII でなければ 401。
    直したら: このテストを `assert resp.status_code == 401` に書き換える。
    """
    resp = _post_raw(ingress, _envelope(_text_event("状況")), signature=raw, allow_500=True)
    assert resp.status_code == 500


def test_現状_シークレット未設定だとpostbackも無検証で通る_既知の穴(ingress, monkeypatch):
    """`if secret:` の fail-open。message 経路は test_web_authz.py で固定済み。

    ここでは postback も同じく素通りすることを固定する。postback は
    炊飯器のロック確認・食事セッションの人物割当まで到達するので、
    偽造ボタン押下で祖母の記録を書き換えられる。

    本来どうあるべきか: シークレットが読めないなら全部 401（fail-closed）。
    直したら: このテストを「401 になり処理に渡らない」に書き換える。
    """
    monkeypatch.setattr(ingress.module, "_load_line_secret", lambda: "")
    resp = _post_raw(ingress, _envelope(_postback_event("feedback_start")), signature=None)
    assert resp.status_code == 200
    assert len(_replies(ingress)) == 1, "署名なしの偽造 postback が処理された"


# ============================================================
# 2. LINE の再送（redelivery）と冪等性
# ============================================================

def test_現状_同じwebhookEventIdを2回受けると2回処理される(ingress):
    """**冪等性の仕組みが無い。** `webhookEventId` を記録するテーブルも無い。

    LINE は 2xx が返らない/遅いと同じ webhook を再送する。いま 500 になる経路
    （下の「壊れたイベント」「dispatch 例外」のテスト）が実際にあるので、
    再送は起こりうる前提。再送されると「意見」が2件、人物割当なら2回記録される。

    本来どうあるべきか: `webhookEventId` を一意キーで記録し、既に見た ID は
    ログだけ残して skip する。
    直したら: このテストを `== 1` に書き換える。
    """
    event = _text_event("意見 テストの意見")
    assert _post(ingress, event).status_code == 200
    assert _post(ingress, event).status_code == 200
    assert _feedback_rows(ingress) == 2, "再送で二重登録されていない（直った？）"


def test_現状_再送フラグisRedeliveryは一切見ていない(ingress):
    """LINE は再送時に `deliveryContext.isRedelivery = true` を立ててくれる。

    これを見るだけでも「再送なら副作用のある操作はしない」と判断できるが、
    コード上このキーはどこからも参照されていない。

    直したら: このテストを「isRedelivery=true なら処理しない」に書き換える。
    """
    first = _text_event("意見 テストの意見", token="test-token-A")
    redelivered = _text_event("意見 テストの意見", token="test-token-A", redelivery=True)
    _post(ingress, first)
    _post(ingress, redelivered)
    assert _feedback_rows(ingress) == 2


def test_現状_再送では同じreplyTokenに2回返信を試みる(ingress):
    """`replyToken` は1回しか使えない。2回目は LINE が 400 で必ず拒否する。

    **症状の本体がこれ。** 1回目の返信が失敗した（＝再送された）ケースでは、
    再送時の返信も使用済みトークンなので必ず失敗し、家族には**何も届かない**。
    DB の副作用だけが2回起きる。
    """
    event = _text_event("ヘルプ", token="test-token-reused")
    _post(ingress, event)
    _post(ingress, event)
    assert _reply_tokens(ingress) == ["test-token-reused", "test-token-reused"]


def test_現状_LINEが返信を拒否しても再送もoutbox退避もされない(ingress):
    """返信が失敗しても webhook は 200 を返す → LINE 側の再送も起きない。

    `reply_line_message` は失敗を WARNING ログに書くだけで、
    `_enqueue_outbox`（push 用の再送キュー）には積まない。
    つまり**返信の失敗は、どこからも回復されず永久に失われる**。
    処理（DB 書き込み）だけが済んで、家族から見ると「無反応」になる。

    本来どうあるべきか: reply が失敗したら push にフォールバックする
    （push は outbox で守られている）。
    直したら: このテストを「outbox に1件積まれる」に書き換える。
    """
    ingress.api["status"] = 400
    ingress.api["text"] = '{"message":"Invalid reply token"}'
    resp = _post(ingress, _text_event("意見 テストの意見"))
    assert resp.status_code == 200
    assert _feedback_rows(ingress) == 1, "処理そのものは済んでいる"
    assert ingress.outbox == [], "返信失敗が outbox に退避されていない（直った？）"


def test_現状_網が落ちて返信が送れなくてもoutboxに積まれない(ingress):
    """上と同じことを、HTTP 応答ではなく通信例外で起こす。

    push 経路 (`send_line_message` / `send_line_with_quick_reply`) は
    ConnectionError/Timeout を outbox に退避するのに、reply 経路だけ素通りで捨てる。
    """
    ingress.api["error"] = OSError("test: network unreachable")
    resp = _post(ingress, _text_event("状況"))
    assert resp.status_code == 200
    assert ingress.outbox == []


def test_登録コマンドは再送されても二重登録にならない(ingress):
    """良い方の確認。`ON CONFLICT(line_user_id) DO UPDATE` が効いている。

    冪等性が無いのに実害が出ない数少ない経路。ここが壊れると
    家族が二重に通知先登録されて同じ通知が2通届く。
    """
    event = _text_event("登録 テスト母", sender=STRANGER)
    _post(ingress, event)
    _post(ingress, event)
    conn = sqlite3.connect(ingress.db)
    persons = conn.execute(
        "SELECT COUNT(*) FROM persons WHERE name = 'テスト母'").fetchone()[0]
    links = conn.execute("SELECT COUNT(*) FROM family_line_users").fetchone()[0]
    conn.close()
    assert (persons, links) == (1, 1)


def test_現状_ロック解除の再送は解除1回だが返信が食い違う(ingress):
    """4桁コードの再送。`_pending_unlocks.pop` のおかげで解除は1回で済む。

    ただし再送時の返信は「確認コードは発行されていません」になる。
    1回目の返信が届いていれば矛盾したメッセージが2通、
    1回目が失敗していた場合は（同じ replyToken なので）こちらも失敗して、
    **解除は成功しているのに家族には何も届かない**。
    """
    import src.line_commands as line_commands

    _lock_rice_cooker(ingress)
    _post(ingress, _text_event("ロック解除", token="test-token-1"))
    code = line_commands._pending_unlocks[REGISTERED]["code"]

    confirm = _text_event(code, token="test-token-2", event_id="TESTEVENT2")
    _post(ingress, confirm)
    _post(ingress, confirm)

    assert ingress.unlocks == ["rice_cooker"], "再送で二重解除されていない"
    replies = _replies(ingress)
    assert replies[0].startswith("🔑 確認コード")
    assert replies[1].startswith("✅")
    assert "確認コードは発行されていません" in replies[2]
    assert _reply_tokens(ingress)[1:] == ["test-token-2", "test-token-2"]


def test_現状_同一バッチ内に重複イベントがあっても2回処理される(ingress):
    """再送ではなく、1つの webhook に同じイベントが2つ入っていた場合。

    ループは `events` をそのまま回すので、バッチ内の重複も素通りで2回処理される。
    """
    event = _text_event("意見 テストの意見")
    resp = _post(ingress, event, dict(event))
    assert resp.status_code == 200
    assert _feedback_rows(ingress) == 2


# ============================================================
# 3. 1つの webhook に複数イベント
# ============================================================

def test_1つのwebhookに入った複数イベントは全部処理される(ingress):
    """正常系。LINE は複数イベントをまとめて1回で送ることがある。"""
    resp = _post(
        ingress,
        _text_event("状況", token="t1", event_id="E1"),
        _text_event("ヘルプ", token="t2", event_id="E2"),
        _text_event("登録一覧", token="t3", event_id="E3"),
    )
    assert resp.status_code == 200
    assert _reply_tokens(ingress) == ["t1", "t2", "t3"]


def test_現状_messageイベントで例外が出ると後続イベントが丸ごと捨てられる(ingress, monkeypatch):
    """**postback 経路には try/except があるのに、message 経路には無い。**

    `reply = await dispatch(text, sender_id)` が素のまま呼ばれているので、
    dispatch 内の未捕捉例外（DB ロック・Matter タイムアウト・`get_conn` 失敗など）が
    そのまま webhook 全体を 500 にする。同じバッチの後続イベントは
    dispatch に渡らないまま消え、LINE はバッチ全体を再送する
    → 先頭のイベントだけ二重処理される。

    本来どうあるべきか: イベント1件ごとに try/except し、失敗しても
    `⚠️ 処理に失敗しました` を返して次のイベントへ進む（postback 側と同じ形）。
    直したら: このテストを「200 で、3件目も dispatch に渡る」に書き換える。
    """
    import src.line_commands as line_commands

    seen: list[str] = []

    async def flaky(text, sender_id):
        seen.append(text)
        if text == "こわれる":
            raise RuntimeError("test: dispatch failure")
        return f"返信:{text}"

    monkeypatch.setattr(line_commands, "dispatch", flaky)
    resp = _post(
        ingress,
        _text_event("1件目", token="t1", event_id="E1"),
        _text_event("こわれる", token="t2", event_id="E2"),
        _text_event("3件目", token="t3", event_id="E3"),
        allow_500=True,
    )
    assert resp.status_code == 500
    assert seen == ["1件目", "こわれる"], "3件目が dispatch に渡っていない"
    assert _reply_tokens(ingress) == ["t1"]


def test_postbackイベントの例外は後続イベントを止めない(ingress, monkeypatch):
    """対比。postback は `except Exception` で拾って `⚠️ 処理に失敗しました` を返し、
    `continue` で次のイベントに進む。message 経路もこの形にすべき。
    """
    import src.line_commands as line_commands

    async def boom(data, sender_id):
        raise RuntimeError("test: postback failure")

    monkeypatch.setattr(line_commands, "handle_confirm_postback", boom)
    resp = _post(
        ingress,
        _postback_event("confirm:1:ok", token="t1", event_id="P1"),
        _postback_event("feedback_start", token="t2", event_id="P2"),
    )
    assert resp.status_code == 200
    replies = _replies(ingress)
    assert replies[0] == "⚠️ 処理に失敗しました"
    assert "意見・質問の送り方" in replies[1]


def test_現状_壊れたイベントが混ざると前半だけ処理されて500になる(ingress):
    """バッチの2件目だけ形が違うケース（`source: null`）。

    1件目の返信は飛び、2件目で 500、3件目は消える。LINE はバッチ全体を再送し、
    1件目はまた処理される（返信は使用済みトークンなので失敗）。
    「同じ操作が2回効いたのに返事は来ない」という最悪の組み合わせ。
    """
    resp = _post(
        ingress,
        _text_event("状況", token="t1", event_id="E1"),
        {"type": "message", "replyToken": "t2", "source": None,
         "message": {"type": "text", "text": "状況"}},
        _text_event("ヘルプ", token="t3", event_id="E3"),
        allow_500=True,
    )
    assert resp.status_code == 500
    assert _reply_tokens(ingress) == ["t1"]


# ============================================================
# 4. 壊れたペイロード
# ============================================================

CRASHING: dict[str, bytes] = {
    "payload自体がlist": b"[]",
    "payload自体がnull": b"null",
    "payload自体が文字列": b'"hello"',
    "eventsがnull": b'{"events":null}',
    "eventsがdict": b'{"events":{"type":"message"}}',
    "eventsが文字列": b'{"events":"message"}',
    "event1件が文字列": b'{"events":["message"]}',
    "event1件がnull": b'{"events":[null]}',
    "sourceがnull": b'{"events":[{"type":"message","source":null,'
                    b'"message":{"type":"text","text":"x"}}]}',
    "messageがnull": b'{"events":[{"type":"message","source":{"userId":"U"},"message":null}]}',
    # 許可リスト判定より後ろの分岐なので、許可済みの送信者で投げる必要がある
    "postbackがnull": ('{"events":[{"type":"postback","source":{"userId":"%s"},'
                      '"postback":null}]}' % REGISTERED).encode(),
    "userIdが数値": b'{"events":[{"type":"message","source":{"userId":12345},'
                  b'"message":{"type":"text","text":"x"}}]}',
    "textが数値": b'{"events":[{"type":"message","source":{"userId":"U"},'
                b'"message":{"type":"text","text":99}}]}',
    "textがnull": b'{"events":[{"type":"message","source":{"userId":"U"},'
                 b'"message":{"type":"text","text":null}}]}',
}


@pytest.mark.parametrize("name", sorted(CRASHING), ids=sorted(CRASHING))
def test_現状_形の違うペイロードは400ではなく500になる(ingress, name):
    """型を確かめずに `.get()` / `[:8]` / `.strip()` するので AttributeError になる。

    どれも LINE が送ってくる形ではないので実害は小さい。ただし
    **500 は LINE に再送させる**ので、固定しておく意味はある
    （再送のたびに同じ 500 → ログが埋まる）。
    `sourceがnull` だけは上の「混在バッチ」テストのとおり、
    手前のイベントの副作用を確定させてから落ちる点で質が違う。

    本来どうあるべきか: イベント単位の try/except で握って次へ進み、
    webhook 自体は 200 を返す。
    直したら: このテストを `== 200` に書き換える。
    """
    resp = _post_raw(ingress, CRASHING[name], allow_500=True)
    assert resp.status_code == 500


QUIET: dict[str, bytes] = {
    "本文が空": b"",
    "eventsキーが無い": b'{"destination":"Ufake"}',
    "eventsが空配列": b'{"destination":"Ufake","events":[]}',
    "typeが無い": b'{"events":[{"source":{"userId":"U"},'
                b'"message":{"type":"text","text":"x"}}]}',
    "typeが数値": b'{"events":[{"type":1,"source":{"userId":"U"}}]}',
    "messageキーが無い": b'{"events":[{"type":"message","source":{"userId":"U"}}]}',
    "sourceキーが無い": b'{"events":[{"type":"message",'
                   b'"message":{"type":"text","text":"x"}}]}',
}


@pytest.mark.parametrize("name", sorted(QUIET), ids=sorted(QUIET))
def test_形が足りないペイロードは200で黙って捨てられる(ingress, name):
    """200 を返して何もしない。LINE コンソールの「検証」ボタンは
    `events: []` を送ってくるので、ここは 200 でよい。

    `sourceキーが無い` だけは sender_id が空文字になり、
    「未許可送信者」として捨てられている（結果は同じだが理由が違う）。
    """
    resp = _post_raw(ingress, QUIET[name])
    assert resp.status_code == 200
    assert ingress.calls == []


@pytest.mark.parametrize(("name", "text", "recognized"), [
    ("絵文字つき", "状況👵🍚", True),
    ("改行入り", "状況\n\n教えて", True),
    ("前後に半角空白", "  状況  ", True),
    ("全角スペース始まり", "　状況", True),
    ("2万文字", "状況" + "あ" * 20000, True),
    ("ゼロ幅スペース混入", "状​況", False),
], ids=lambda v: str(v))
def test_本文の形による取りこぼし(ingress, name, text, recognized):
    """絵文字・改行・前後の空白・全角スペースは `strip()` と `in` 判定を通る。

    通らないのはゼロ幅スペース (U+200B) で、`"状況" in text` が False になり
    **コマンドとしては認識されない**。コピペや一部の IME で混入しうる。
    2026-10-15 まではそのまま無言だったが、今は案内が返るので
    家族は「打ち直せばいい」と分かる。認識自体を直すなら正規化が要る。
    """
    resp = _post(ingress, _text_event(text))
    assert resp.status_code == 200
    replies = _replies(ingress)
    assert replies, "どの形でも最低限の返信はあること"
    body = str(replies[-1])
    if recognized:
        assert "分かりませんでした" not in body, "コマンドとして認識されること"
    else:
        assert "分かりませんでした" in body, "認識できないなら案内を返すこと"


def test_イベントが大量に入っていても順番に処理される(ingress):
    """LINE は最大でも数十件をまとめる。打ち切りや取りこぼしが無いこと。"""
    events = [_text_event("ヘルプ", token=f"t{i}", event_id=f"E{i}") for i in range(40)]
    resp = _post(ingress, *events)
    assert resp.status_code == 200
    assert len(_replies(ingress)) == 40


# ============================================================
# 5. イベント種別 — text 以外は黙って落ちる
# ============================================================

def _msg(message: dict, source=None) -> dict:
    return {"type": "message", "webhookEventId": "E1", "replyToken": "t1",
            "source": source or {"type": "user", "userId": REGISTERED},
            "message": message}


NON_TEXT_MESSAGES: dict[str, dict] = {
    "image": _msg({"type": "image", "id": "1"}),
    "sticker": _msg({"type": "sticker", "id": "1", "packageId": "1", "stickerId": "2"}),
    "audio": _msg({"type": "audio", "id": "1", "duration": 1000}),
    "video": _msg({"type": "video", "id": "1"}),
    "file": _msg({"type": "file", "id": "1", "fileName": "test.pdf", "fileSize": 1}),
    "location": _msg({"type": "location", "id": "1", "latitude": 35.0, "longitude": 139.0}),
}


@pytest.mark.parametrize("kind", sorted(NON_TEXT_MESSAGES), ids=sorted(NON_TEXT_MESSAGES))
def test_text以外のメッセージは黙って捨てられる_エラーにはならない(ingress, kind):
    """家族がスタンプ・写真・音声・位置情報を送った場合。

    返信が無いのは仕様として妥当（食事写真はタブレットUIから送る）。
    ここで確認したいのは**例外にならないこと**。500 になると LINE が
    同じスタンプを再送し続け、ログが埋まる。

    なお「写真を送れば記録されるのでは」と家族が思うと無反応に見える。
    返信しないなら、せめて1回だけ案内を返すのが親切（FAMILY_GUIDE.md 側の話）。
    """
    resp = _post(ingress, NON_TEXT_MESSAGES[kind])
    assert resp.status_code == 200
    assert ingress.calls == []


OTHER_EVENTS: dict[str, dict] = {
    "follow": {"type": "follow", "replyToken": "t", "source": {"type": "user", "userId": STRANGER}},
    "unfollow": {"type": "unfollow", "source": {"type": "user", "userId": REGISTERED}},
    "join": {"type": "join", "replyToken": "t", "source": {"type": "group", "groupId": GROUP}},
    "leave": {"type": "leave", "source": {"type": "group", "groupId": GROUP}},
    "memberJoined": {"type": "memberJoined", "replyToken": "t",
                     "source": {"type": "group", "groupId": GROUP},
                     "joined": {"members": [{"type": "user", "userId": STRANGER}]}},
    "memberLeft": {"type": "memberLeft", "source": {"type": "group", "groupId": GROUP},
                   "left": {"members": [{"type": "user", "userId": STRANGER}]}},
    "beacon": {"type": "beacon", "replyToken": "t",
               "source": {"type": "user", "userId": REGISTERED},
               "beacon": {"hwid": "testhw", "type": "enter"}},
    "accountLink": {"type": "accountLink", "replyToken": "t",
                    "source": {"type": "user", "userId": REGISTERED},
                    "link": {"result": "ok", "nonce": "testnonce"}},
    "unsend": {"type": "unsend", "source": {"type": "user", "userId": REGISTERED},
               "unsend": {"messageId": "1"}},
    "videoPlayComplete": {"type": "videoPlayComplete", "replyToken": "t",
                          "source": {"type": "user", "userId": REGISTERED},
                          "videoPlayComplete": {"trackingId": "1"}},
    "未知の新イベント": {"type": "somethingNew2027", "replyToken": "t",
                 "source": {"type": "user", "userId": REGISTERED}},
}


@pytest.mark.parametrize("kind", sorted(OTHER_EVENTS), ids=sorted(OTHER_EVENTS))
def test_message_postback以外のイベント種別は黙って捨てられる(ingress, kind):
    """`if ev_type != "message": continue` で全部落ちる。例外にはならない。

    unfollow（ブロック）だけは拾う価値がある: 家族が bot をブロックすると
    以降その人への push は全部失敗するのに、`family_line_users` には
    登録が残り続け、`broadcast` のたびに失敗を積む。
    """
    resp = _post(ingress, OTHER_EVENTS[kind])
    assert resp.status_code == 200
    assert ingress.calls == []


def test_現状_友だち追加followに歓迎メッセージが返らない(ingress):
    """新しい家族が bot を友だち追加した瞬間。

    `follow` は捨てられるので、何の案内も返らない。
    許可リスト方式なので、この人は「登録」を自分で送らない限り
    **以降どのコマンドも無言で無視される**。つまり最初の接点で詰む。
    「反応しない」の報告が新しい家族から来た場合、まずこれを疑う。

    本来どうあるべきか: follow に「登録 <名前> と送ってください」を返す。
    直したら: このテストを「案内が1通返る」に書き換える。
    """
    resp = _post(ingress, OTHER_EVENTS["follow"])
    assert resp.status_code == 200
    assert ingress.calls == []


@pytest.mark.parametrize("data", ["", "unknown:1", "attribute", "ATTRIBUTE:1:2",
                                  " attribute:1:2"], ids=lambda v: repr(v))
def test_見覚えのないpostbackデータは黙って捨てられる(ingress, data):
    """`data.startswith(...)` の分岐に当たらないと `reply` は None のまま。

    大文字違い・先頭スペース・プレフィックスだけ、はすべて無反応。
    古い通知のボタンを後から押した場合もここに落ちる（家族は押したのに無反応）。
    """
    resp = _post(ingress, _postback_event(data))
    assert resp.status_code == 200
    assert ingress.calls == []


def test_dataの無いpostbackも黙って捨てられる(ingress):
    """日時選択アクションは `postback.params` だけで `data` が空のことがある。"""
    event = _postback_event("")
    event["postback"] = {"params": {"date": "2026-10-15"}}
    resp = _post(ingress, event)
    assert resp.status_code == 200
    assert ingress.calls == []


# ============================================================
# 6. 送信元の種別 — group / room
# ============================================================

@pytest.mark.parametrize(("source", "expected"), [
    ({"type": "user", "userId": REGISTERED}, REGISTERED),
    ({"type": "group", "groupId": GROUP, "userId": REGISTERED}, GROUP),
    ({"type": "group", "groupId": GROUP}, GROUP),
    ({"type": "room", "roomId": ROOM, "userId": REGISTERED}, ROOM),
    ({}, ""),
], ids=["user", "group+userId", "group", "room+userId", "空"])
def test_extract_sender_idはgroupIdとroomIdをuserIdより優先する(ingress, source, expected):
    """`source.get("groupId") or source.get("roomId") or source.get("userId", "")`。

    グループからの発言では、**誰が言ったか (userId) が捨てられて**
    グループの ID が sender_id になる。下のテストがその帰結。
    """
    assert ingress.module._extract_sender_id(source) == expected


@pytest.mark.parametrize("source", [
    {"type": "group", "groupId": GROUP, "userId": REGISTERED},
    {"type": "group", "groupId": GROUP},
    {"type": "room", "roomId": ROOM, "userId": REGISTERED},
], ids=["group+userId", "group", "room+userId"])
def test_現状_登録済み家族がグループで送ると無視される(ingress, source):
    """**症状の最有力候補。**

    許可リストに入っているのは個人の user ID。グループ発言では sender_id が
    groupId になるので一致せず、「未許可送信者」として `continue` で捨てられる。
    返信も無く、ログに残るのも `未許可送信者: <groupId>` の1行だけ。

    家族から見ると「1対1のトークでは反応するのに、家族グループでは無反応」。
    userId は同じ人なのに通らない。

    本来どうあるべきか: 許可判定は常に `source.userId`（個人）で行い、
    返信先の決定だけ group/room を使う。
    直したら: このテストを「dispatch に渡り返信が1通返る」に書き換える。
    """
    resp = _post(ingress, _text_event("状況", source=source))
    assert resp.status_code == 200
    assert ingress.calls == [], "グループ発言が処理された（直った？）"


def test_現状_グループで押したQuickReplyボタンも無視される(ingress):
    """許可リスト判定は postback 分岐より手前にあるので、ボタンも同じく落ちる。

    通知をグループに送る運用にすると、確認ボタンが全員ぶん効かなくなる。
    """
    resp = _post(ingress, _postback_event(
        "feedback_start", source={"type": "group", "groupId": GROUP, "userId": REGISTERED}))
    assert resp.status_code == 200
    assert ingress.calls == []


def test_現状_グループからの登録はgroupIdを家族として登録してしまう(ingress):
    """「登録」は許可リストを免除されるので、グループからでも通る。

    その結果 `family_line_users.line_user_id` に**グループ ID が入る**。
    以降そのグループは1人の家族として扱われ、
    個々のメンバーの1対1トークは相変わらず無視される。
    """
    _post(ingress, _text_event(
        "登録 テスト叔父", source={"type": "group", "groupId": GROUP, "userId": STRANGER}))
    conn = sqlite3.connect(ingress.db)
    stored = [r[0] for r in conn.execute("SELECT line_user_id FROM family_line_users")]
    conn.close()
    assert stored == [GROUP], "個人ではなくグループ ID が登録される"


def test_現状_グループが許可されるとメンバー全員が1人の送信者になる(ingress, monkeypatch):
    """グループ ID が許可リストに入ると動く。ただし全員が同一人物扱いになる。

    ロック解除コードは `_pending_unlocks[sender_id]` で持つので、
    **A さんに出したコードを B さんが使える**（コードはグループに流れるので全員に見える）。
    4桁コードの意味が無くなる。
    """
    import src.line_commands as line_commands

    monkeypatch.setattr(ingress.module, "_load_line_allowed_senders", lambda: {GROUP})
    _lock_rice_cooker(ingress)

    _post(ingress, _text_event(
        "ロック解除", token="t1",
        source={"type": "group", "groupId": GROUP, "userId": REGISTERED}))
    assert set(line_commands._pending_unlocks) == {GROUP}, "個人ではなくグループに紐づく"
    code = line_commands._pending_unlocks[GROUP]["code"]

    _post(ingress, _text_event(
        code, token="t2", event_id="E2",
        source={"type": "group", "groupId": GROUP, "userId": STRANGER}))
    assert ingress.unlocks == ["rice_cooker"], "別のメンバーが他人のコードで解除できた"


def test_現状_未登録者がグループから登録すると炊飯器を操作できる(ingress, monkeypatch):
    """`登録` の許可リスト免除と groupId 優先が噛み合うと、認可が一段抜ける。

    bot は友だちなら誰でもグループに招待できる。未登録の第三者が
    bot と自分だけのグループを作って「登録 なまえ」を送ると、
    そのグループ ID が許可リストに入り、以降その人は
    「ロック解除」まで到達できる（本人の user ID は一度も許可されていない）。

    本来どうあるべきか: 登録は 1:1 トーク (`source.type == "user"`) のみ受け付ける。
    直したら: このテストを「グループからの登録は拒否される」に書き換える。
    """
    monkeypatch.setattr(ingress.module, "_load_line_allowed_senders",
                        _allowed_from_db(ingress))
    _lock_rice_cooker(ingress)
    source = {"type": "group", "groupId": GROUP, "userId": STRANGER}

    _post(ingress, _text_event("登録 テスト叔父", token="t1", source=source))
    _post(ingress, _text_event("ロック解除", token="t2", event_id="E2", source=source))

    replies = _replies(ingress)
    assert replies[0].startswith("✅ 「テスト叔父」として登録しました")
    assert replies[1].startswith("🔑 確認コード"), "未許可の第三者に解除コードが出た"


def test_source欠落は空のsender_idになり許可リストで落ちる(ingress):
    """`source` が無い/空のとき sender_id は空文字。許可リストに無いので捨てられる。

    許可リストが空（fail-open）のときだけ、空の sender_id でも通ってしまう。
    その場合 `handle_register_family` は空文字を line_user_id として登録しうる。
    """
    resp = _post(ingress, _text_event("状況", source={}))
    assert resp.status_code == 200
    assert ingress.calls == []


# ============================================================
# 7. 返信経路の非対称 — どのコマンドが黙るか
# ============================================================

@pytest.mark.parametrize("text", ["ありがとう", "おはよう", "ご飯食べた？", "状",
                                  "すてーたす", "？", "👍", "元気？"], ids=lambda v: v)
def test_認識できない本文には案内を返す(ingress, text):
    """`match_command` が unknown でも無言で終わらせない。

    **「反応するときとしないときがある」の素の正体がこれだった。**
    2026-10-15 まで「状況」は通るのに「元気？」「ありがとう」は1文字も返らず、
    家族から見ると同じ LINE に送ったのに返事が来たり来なかったりしていた。
    実測で入力 272 パターン中 93 がここに落ちていた。

    正しい言い方への手がかりを必ず含めること。それが無いと家族は学習できない。
    """
    resp = _post(ingress, _text_event(text))
    assert resp.status_code == 200
    assert ingress.calls, "未知の入力にも案内を返すこと"
    assert "ヘルプ" in str(ingress.calls[-1]), "ヘルプへの誘導を含めること"


def test_メニューだけはreplyではなくpushで送られる(ingress):
    """`handle_menu` は dict を返し、webhook は `send_line_with_quick_reply`（push）で送る。

    他のコマンドは reply。この違いが次のテストの原因になる。
    """
    resp = _post(ingress, _text_event("メニュー"))
    assert resp.status_code == 200
    assert _urls(ingress) == [PUSH_API]


def test_現状_メニューはマスタースイッチOFFで無反応になる(ingress):
    """**症状の有力候補。** `notify_master_enabled` の説明文には
    「OFFで全Push通知を停止。**Webhook返信は継続**」と書いてある
    (src/settings.py)。ところが「メニュー」だけが push 実装なので、
    マスタースイッチ OFF のときユーザ起点の操作まで黙る。

    家族から見ると「『状況』は返るのに『メニュー』だけ無反応」。
    しかも押した側には何も表示されず、ログにも
    「LINE通知マスタースイッチOFF → Quick Reply送信スキップ」しか残らない。

    本来どうあるべきか: webhook への返信はマスタースイッチの対象外にする
    （reply で送る、または push 時に `is_reply=True` で素通りさせる）。
    直したら: このテストを「OFF でもメニューが届く」に書き換える。
    """
    ingress.switches["notify_master_enabled"] = False
    resp = _post(ingress, _text_event("メニュー"))
    assert resp.status_code == 200
    assert ingress.calls == [], "メニューが送信された（直った？）"


@pytest.mark.parametrize("text", ["状況", "ヘルプ", "登録一覧"], ids=lambda v: v)
def test_マスタースイッチOFFでも他のコマンドは返信が届く(ingress, text):
    """対比。reply 経路はマスタースイッチを見ないので動く＝挙動が食い違う。"""
    ingress.switches["notify_master_enabled"] = False
    resp = _post(ingress, _text_event(text))
    assert resp.status_code == 200
    assert len(_replies(ingress)) == 1


def test_replyTokenが空でもpushで届く(ingress):
    """`reply_line_message` は reply_token が空なら API を叩かず False を返す。

    2026-10-15 まで webhook は戻り値を捨てていたため、処理は済んでいるのに
    家族には何も届かなかった。今は False を受けて push に切り替える。
    """
    resp = _post(ingress, _text_event("意見 テストの意見", token=""))
    assert resp.status_code == 200
    assert _feedback_rows(ingress) == 1, "処理は済んでいる"
    assert ingress.calls, "reply が使えないなら push で送ること"


def test_リンク要求の返信にタブレットトークンが入る(ingress):
    """`_build_url_reply` は返信本文に TABLET_TOKEN 付き URL を埋める。

    つまり「つながらない」等のキーワードを含む発言に反応して、
    **タブレットの認証トークンが LINE のトーク履歴に残る**。
    許可リストの内側なので外部には出ないが、グループが許可されている場合は
    グループ全員に配られる（上のグループ系テスト参照）。

    ここでは漏れ先の確認だけ。トークンは架空値に差し替えてある。
    """
    resp = _post(ingress, _text_event("リンク"))
    assert resp.status_code == 200
    assert TABLET_TOKEN in _replies(ingress)[0]


# ============================================================
# 8. 許可リストの取得そのもの
# ============================================================

def test_現状_許可リストのDB参照が落ちると登録済み家族が黙って消える(ingress, monkeypatch):
    """`_load_line_allowed_senders` の DB 参照は `except Exception: pass`。

    `登録` で増えた家族は DB 側にしか居ないので、DB が一時的に掴めない
    （SQLITE_BUSY / WAL のロック競合 / ディスク詰まり）瞬間に、
    その人たちだけ許可リストから消える。リクエスト単位で読み直すので、
    **同じ人の同じコマンドが、送るタイミングで通ったり落ちたりする**。
    これも「反応するときとしないときがある」を説明しうる。

    `.env` 側も読めないと許可リストは空になり、今度は逆に全員通る
    （fail-open。test_web_authz.py で固定済み）。

    本来どうあるべきか: DB 参照の失敗は ERROR ログに出し、
    直前に成功した集合をキャッシュして使う（黙って縮めない）。
    直したら: このテストを「DB が落ちても DB 登録者が残る」に書き換える。
    """
    import src.db as db

    # BASE を差し替えてあるので、ここで置く .env だけが読まれる（本番は読まない）
    (ingress.env_dir / ".env").write_text(f"LINE_ALLOWED_SENDERS={REGISTERED}\n")
    _register_in_db(ingress, DB_ONLY)

    healthy = ingress.real_allowed()
    assert REGISTERED in healthy and DB_ONLY in healthy, "正常時は両方見える"

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("test: database is locked")

    real_get_conn = db.get_conn
    monkeypatch.setattr(db, "get_conn", broken)
    degraded = ingress.real_allowed()
    assert REGISTERED in degraded, ".env 側は残る"
    assert DB_ONLY not in degraded, "DB 登録の家族が黙って消える（直った？）"

    # 縮んだ許可リストでリクエストが来たときの、家族から見た挙動。
    # DB 自体は戻す（この先は webhook の経路だけを見たい）。
    monkeypatch.setattr(db, "get_conn", real_get_conn)
    monkeypatch.setattr(ingress.module, "_load_line_allowed_senders", lambda: degraded)
    resp = _post(ingress, _text_event("状況", sender=DB_ONLY))
    assert resp.status_code == 200
    assert ingress.calls == [], "登録済みの家族が無言で無視される"
