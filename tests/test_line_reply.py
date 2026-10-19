"""LINE の「返信・送信が本当に届くか」を見るテスト。

起点は「LINE のコマンドが反応するときとしないときがある」。
受信・コマンド解釈までは別ファイルが見ているので、ここは**出口**だけを見る。

このファイルが相手にしている構造的な穴:

1. **返信の成否を誰も見ていない。** webhook は
   `await asyncio.to_thread(reply_line_message, reply_token, reply)` の戻り値を捨てる。
   `reply_line_message` は失敗しても例外を投げず False を返すだけなので、
   webhook 側の `except` にも掛からない。LINE の reply token は1回限り・約1分で失効する
   ので、LINE が webhook を再送した2通目、通信が一瞬切れた1通、本文が 5000 文字を
   超えた1通は、**家族から見ると「無反応」になる**。push へのフォールバックは無い。
2. **reply と push でマスタースイッチの扱いが違う。** 「メニュー」だけが push
   (`send_line_with_quick_reply`) で送られ、こちらは `notify_master_enabled` を見る。
   つまりスイッチを切ると「ヘルプ」は返るのに「メニュー」だけ無言になる。
   「反応するときとしないときがある」の見え方そのもの。
3. **退避されるのはネット断だけ。** HTTP 400/401/429/500 は outbox に積まれず
   その場で捨てられる。reply はネット断でも積まれない（積んでも token が死んでいる）。
4. **outbox の再送は壊れた1行で止まる。** `entry["to"]` が無い行で KeyError が出ると
   main() が落ち、ファイルは書き戻されない。その結果、手前の行が毎分再送され続け、
   retry_count も TTL も進まない。

テストはすべて偽の LINE API (`_FakeLineAPI`) に向かって走る。
`notifier.requests.post` を差し替えているので、本物の api.line.me は一度も叩かない
（家族5人の端末に通知が飛ぶため絶対に叩けない）。宛先・トークンは全部架空。

**現状を固定している characterization テストには、その旨と「本来どうあるべきか」を
docstring に書いてある。** 実装は直していない（このタスクでは src/ を変更しない）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
import json
import logging
import sqlite3
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from src import line_commands, notifier
import scripts.retry_line_outbox as retry_outbox

ROOT = Path(__file__).resolve().parent.parent

# --- 架空の資格情報・宛先。実在の値は書かない -------------------------------
LINE_SECRET = "test-line-channel-secret-not-real"
FAKE_TOKEN = "dummy-channel-access-token-for-tests"
ADMIN = "Utest_admin_0000"          # .env の LINE_USER_ID 相当（孫=admin）
FAMILY_A = "Utest_family_0001"      # LINE_ALLOWED_SENDERS に居る家族
FAMILY_B = "Utest_family_0002"      # DB(family_line_users) だけに居る家族
STRANGER = "Utest_stranger_9999"

FAKE_ENV = {
    "LINE_CHANNEL_ACCESS_TOKEN": FAKE_TOKEN,
    "LINE_USER_ID": ADMIN,
    "LINE_ALLOWED_SENDERS": f"{ADMIN},{FAMILY_A}",
}

PUBLIC_BASE = "https://test.example.invalid"   # 公開URL（架空・到達不能ドメイン）

# 本番の退避ファイル。テストで絶対に作ってはいけない
# （作ると毎分の cron が本物の LINE に送ろうとする）。
PRODUCTION_OUTBOX = ROOT / "data" / "line_outbox.jsonl"


# ============================================================
# 偽 LINE API — 実 API の「落とす条件」だけを真似る
# ============================================================

class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeLineAPI:
    """api.line.me の代わり。実 API が 400 を返す条件を再現する。

    再現している制約（LINE Messaging API のドキュメント準拠）:
      - reply token は1回限り（2回目は 400 Invalid reply token）
      - push は `to` が空なら 400
      - text は 5000 文字まで
      - quickReply.items は 1〜13 個。0 個も 14 個以上も 400
      - uri アクションは http/https 必須（相対パスは 400）
      - label は 20 文字まで

    `force_status` / `raise_exc` で任意の失敗を起こせる。
    `fail_for` に宛先を入れると、その宛先だけ 400 になる（一部失敗の再現）。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []        # 試みた全リクエスト
        self.deliveries: list[dict] = []   # 実際に届いたもの
        self.used_reply_tokens: set[str] = set()
        self.force_status: int | None = None
        self.raise_exc: Exception | None = None
        self.fail_for: set[str] = set()
        self.on_call = None                # 送信中の割り込みを再現するフック

    # -- requests.post 互換 ---------------------------------------------
    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        body = json or {}
        api = "reply" if url.endswith("/message/reply") else "push"
        self.calls.append({"api": api, "url": url, "body": body})
        if self.on_call is not None:
            self.on_call()
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.force_status is not None:
            return _Resp(self.force_status, '{"message":"forced by test"}')

        auth = (headers or {}).get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:].strip():
            return _Resp(401, '{"message":"Authentication failed"}')

        messages = body.get("messages") or []
        if not messages or len(messages) > 5:
            return _Resp(400, '{"message":"Invalid messages"}')
        for m in messages:
            if m.get("type") == "text":
                text = m.get("text", "")
                if not text:
                    return _Resp(400, '{"message":"May not be empty"}')
                if len(text) > 5000:
                    return _Resp(400, '{"message":"Length must be between 1 and 5000"}')
            qr = m.get("quickReply")
            if qr is not None:
                items = qr.get("items") or []
                if not items:
                    return _Resp(400, '{"message":"quickReply.items may not be empty"}')
                if len(items) > 13:
                    return _Resp(400, '{"message":"quickReply.items size must be between 1 and 13"}')
                for it in items:
                    action = it.get("action", {})
                    if len(str(action.get("label", ""))) > 20:
                        return _Resp(400, '{"message":"label is too long"}')
                    if action.get("type") == "uri":
                        uri = str(action.get("uri", ""))
                        if not uri.startswith(("http://", "https://")):
                            return _Resp(400, '{"message":"invalid uri scheme"}')

        if api == "reply":
            token = body.get("replyToken", "")
            if not token:
                return _Resp(400, '{"message":"Invalid reply token"}')
            if token in self.used_reply_tokens:
                return _Resp(400, '{"message":"Invalid reply token"}')
            self.used_reply_tokens.add(token)
            dest = None
        else:
            dest = body.get("to", "")
            if not dest:
                return _Resp(400, '{"message":"Failed to send messages"}')
            if dest in self.fail_for:
                return _Resp(400, '{"message":"The destination user has blocked the bot"}')

        self.deliveries.append({
            "api": api,
            "to": dest,
            "texts": [m.get("text", "") for m in messages if m.get("type") == "text"],
            "quick": [
                it.get("action", {})
                for m in messages
                for it in ((m.get("quickReply") or {}).get("items") or [])
            ],
        })
        return _Resp(200)

    # -- 読みやすくするための派生ビュー -----------------------------------
    @property
    def texts(self) -> list[str]:
        return [t for d in self.deliveries for t in d["texts"]]

    @property
    def pushed_to(self) -> list[str]:
        return [d["to"] for d in self.deliveries if d["api"] == "push"]

    @property
    def attempted_apis(self) -> list[str]:
        return [c["api"] for c in self.calls]


# ============================================================
# 足場
# ============================================================

@pytest.fixture(autouse=True)
def _never_touch_production_outbox():
    """本番の退避ファイルをテストが作っていないことを毎回確かめる。

    `notifier._enqueue_outbox` の書き込み先は相対パス `data/line_outbox.jsonl` で、
    プロセスの CWD 次第で本番の退避先になる。テストが1行でも積むと、
    毎分の cron (`scripts/retry_line_outbox.py`) が本物の LINE に送ってしまう。
    """
    existed = PRODUCTION_OUTBOX.exists()
    yield
    if not existed:
        assert not PRODUCTION_OUTBOX.exists(), (
            f"テストが本番の退避ファイルを作った: {PRODUCTION_OUTBOX}\n"
            "outbox を触るテストは必ず monkeypatch.chdir() で CWD を一時ディレクトリへ移すこと。"
        )


@pytest.fixture(autouse=True)
def line_api(monkeypatch):
    """LINE API と本番 .env を遮断し、偽 API を返す。

    notifier は毎回 `.env` を読んで実トークンと実ユーザーIDを取るので、
    `_load_env` ごと差し替える。これを忘れると本物の家族に通知が飛ぶ。
    """
    api = _FakeLineAPI()
    monkeypatch.setattr(notifier.requests, "post", api.post)
    monkeypatch.setattr(notifier.requests, "put", lambda *a, **k: _Resp(200))
    monkeypatch.setattr(notifier, "_load_env", lambda: dict(FAKE_ENV))
    monkeypatch.setattr(notifier, "_admin_display_name", lambda: "テスト管理者")
    # 深夜帯に走らせても結果が変わらないようにする（1〜5時は通知が抑制される）
    monkeypatch.setattr(notifier, "_is_night_quiet_hours", lambda: False)
    return api


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    """`_enqueue_outbox` の相対パス書き込みを一時ディレクトリに閉じ込める。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path / "data" / "line_outbox.jsonl"


def _read_outbox(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# --- webhook（reply/push の経路を端から端まで通す） --------------------------

def _line_body(text: str, sender: str = FAMILY_A,
               reply_token: str = "test-reply-token-1") -> bytes:
    return json.dumps({
        "events": [{
            "type": "message",
            "replyToken": reply_token,
            "source": {"userId": sender},
            "message": {"type": "text", "text": text},
        }]
    }).encode()


def _postback_body(data: str, sender: str = FAMILY_A,
                   reply_token: str = "test-reply-token-1") -> bytes:
    return json.dumps({
        "events": [{
            "type": "postback",
            "replyToken": reply_token,
            "source": {"userId": sender},
            "postback": {"data": data},
        }]
    }).encode()


def _sig(body: bytes) -> str:
    return base64.b64encode(
        hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()


@pytest.fixture
def webhook(temp_db, monkeypatch):
    """署名と許可リストだけ架空に差し替えた、本物の webhook クライアント。

    dispatch も notifier も本物を通す（出口の挙動を見るのが目的）。
    DB は temp_db なので本番には触らない。
    """
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "_load_line_secret", lambda: LINE_SECRET)
    monkeypatch.setattr(app_module, "_load_line_allowed_senders",
                        lambda: {ADMIN, FAMILY_A})
    monkeypatch.setattr(app_module.limiter, "enabled", False)
    # handle_menu が本番の data/tunnel_url.txt を読むので架空の URL に差し替える
    monkeypatch.setattr(line_commands, "_load_public_base_url", lambda: PUBLIC_BASE)

    with TestClient(app_module.app, client=("203.0.113.9", 50000)) as client:
        def post(body: bytes):
            return client.post("/line/webhook", content=body,
                               headers={"x-line-signature": _sig(body)})
        post.client = client
        yield post


# ============================================================
# 1. replyToken のライフサイクル
# ============================================================

def test_LINEが同じwebhookを再送しても2通目がpushで届く(webhook, line_api):
    """reply token は1回限り。LINE の再送で2回目の reply は必ず 400 になる。

    LINE は webhook の応答が遅い/5xx のとき同じ本文を再送する。再送に付いてくる
    replyToken は1通目と同じなので、2回目の返信は API に拒否される。
    2026-10-15 まで実装は失敗を捨てており、家族の端末には何も出なかった。
    **これが「反応するときとしないときがある」の最短経路だった。**
    今は reply が落ちたら push に切り替えるので、2回目も届く。
    """
    body = _line_body("ヘルプ")
    first = webhook(body)
    second = webhook(body)      # LINE の再送（本文も署名も token も同一）

    assert first.status_code == 200 and second.status_code == 200
    assert "push" in line_api.attempted_apis, "2通目は push で送り直すこと"
    assert len(line_api.deliveries) >= 2, "再送ぶんも家族に届くこと"

def test_返信が失敗したらpushで送り直す(webhook, line_api):
    """reply token は1回限り・約1分で失効する。

    LINE が webhook を再送すると2通目は使用済みトークンになり必ず 400 になる。
    2026-10-15 まで webhook は戻り値を捨てており、処理だけ走って家族には
    何も届かなかった。これが「反応するときとしないときがある」の一因。
    """
    line_api.force_status = 400
    resp = webhook(_line_body("ヘルプ"))

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert "push" in line_api.attempted_apis, "reply が落ちたら push で送り直すこと"


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_返信が失敗してもwebhookは200を返す(webhook, line_api, status):
    """返信の失敗が webhook の応答に一切出ないことの固定。

    HTTP 200 を返すのは LINE の再送を防ぐためで、それ自体は妥当。
    ただし「届かなかった」という情報がここで完全に消える。
    """
    line_api.force_status = status
    resp = webhook(_line_body("ヘルプ"))
    assert resp.status_code == 200
    assert line_api.deliveries == []


def test_返信が落ちたらwebhook側にも記録が残る(webhook, line_api, caplog):
    """「反応しない」と言われたときにログから原因を追えること。

    2026-10-15 まで残るのは notifier の `LINE返信失敗: 400 ...` 1行だけで、
    しかも iot-web には logging の設定自体が無く、どこにも出力されていなかった。
    今は webhook 側にも「push で再送」の警告が残るので、RECV 行と突き合わせて
    どの sender のどのコマンドが落ちたのかを追える。
    """
    line_api.force_status = 400
    with caplog.at_level(logging.INFO):
        webhook(_line_body("ヘルプ"))

    notifier_msgs = [r.getMessage() for r in caplog.records if r.name == "notifier"]
    webhook_warn = [r.getMessage() for r in caplog.records
                    if r.name == "line_webhook" and r.levelno >= logging.WARNING]
    assert any("LINE返信失敗" in m and "400" in m for m in notifier_msgs), notifier_msgs
    assert webhook_warn, "webhook 側にも記録を残すこと"

def test_reply_tokenが空なら送信を試みない(line_api):
    """空の reply token で API を叩いても確実に 400。手前で止めるのが正しい。"""
    assert notifier.reply_line_message("", "テスト本文") is False
    assert line_api.calls == []


def test_1イベントに対する返信は1回だけ(webhook, line_api):
    """1つの webhook 内で同じ token を2回使っていないことの確認。

    reply token は1回限りなので、1イベントに2通返そうとすると2通目が消える。
    現状の実装は「メニューなら push、それ以外は reply 1回」で分岐しているので
    二重使用は起きない。この性質が壊れたらここで気づく。
    """
    webhook(_line_body("ヘルプ"))
    assert line_api.attempted_apis == ["reply"]
    assert len(line_api.used_reply_tokens) == 1


def test_postbackの返信も1回限りのreplyに乗る(webhook, line_api):
    """ボタン押下（postback）の返信も reply 経路。

    「了解」ボタンを押したのに反応が無い、という訴えも同じ原因で起きうる。
    """
    webhook(_postback_body("feedback_start"))
    assert line_api.attempted_apis == ["reply"]
    assert "意見・質問の送り方" in line_api.texts[0]


# ============================================================
# 2. reply と push の使い分け
# ============================================================

def test_テキスト返信はreply_APIで送られる(webhook, line_api):
    webhook(_line_body("ヘルプ"))
    assert line_api.calls[0]["url"].endswith("/message/reply")
    assert "to" not in line_api.calls[0]["body"]


def test_メニューだけはpush_APIで送信者宛に送られる(webhook, line_api):
    """「メニュー」は Quick Reply を使うため reply ではなく push。

    push は reply token の失効と無関係なので再送に強いが、
    送信枠を消費し、かつ下のテストの通りマスタースイッチの影響を受ける。
    """
    webhook(_line_body("メニュー"))
    assert line_api.calls[0]["url"].endswith("/message/push")
    assert line_api.pushed_to == [FAMILY_A]
    assert len(line_api.deliveries[0]["quick"]) == 6


def test_マスタースイッチOFFでもテキスト返信は届く(webhook, line_api):
    """`reply_line_message` はマスタースイッチを見ない（意図通り）。

    「通知を止めている間もコマンドには答える」のは妥当な設計。
    問題は次のテストとの食い違い。
    """
    from src.settings import set_setting
    set_setting("notify_master_enabled", "0")

    webhook(_line_body("ヘルプ"))
    assert len(line_api.deliveries) == 1


def test_マスタースイッチOFFだとメニューだけが黙って無反応になる(webhook, line_api):
    """**reply と push でスイッチの扱いが違う**ことの固定。

    `send_line_with_quick_reply` は `notify_master_enabled` を見て False を返す。
    webhook は戻り値を見ないので、家族には「ヘルプは返るのにメニューは無反応」と見える。
    スイッチは `scripts/toggle_notifications.py` で切り替えられ、過去に実際に
    OFF にして運用していた期間がある。

    本来: コマンドへの応答は通知ではないので、メニューもスイッチに関係なく返すべき
    （あるいは「通知を止めています」と答えるべき）。直したら
    `assert len(line_api.deliveries) == 1` に書き換える。
    """
    from src.settings import set_setting
    set_setting("notify_master_enabled", "0")

    resp = webhook(_line_body("メニュー"))
    assert resp.status_code == 200
    assert line_api.calls == [], "スイッチOFFなのに送信を試みた"
    assert line_api.deliveries == []


def test_公開URLが空だとメニューのボタンが相対パスになり弾かれる(webhook, line_api, monkeypatch):
    """`data/tunnel_url.txt` が空/欠落したときメニューが丸ごと消える。

    `handle_menu` は `f"{base_url}/family"` を作るだけなので、base_url が空だと
    uri アクションが `"/family"` になる。LINE は http/https 以外の uri を 400 で
    拒否するため、**メニュー全体が届かない**。ボタン1個の不備で全体が落ちる。
    URL ファイルはトンネル再起動時に書き換わるので、空になる瞬間がありうる。

    本来: uri が http/https でない項目は組み立て時に捨てる（または base_url が
    空ならメニューを出さずにテキストで案内する）。
    """
    monkeypatch.setattr(line_commands, "_load_public_base_url", lambda: "")
    resp = webhook(_line_body("メニュー"))

    assert resp.status_code == 200
    assert len(line_api.calls) == 1, "送信は試みている"
    assert line_api.deliveries == [], "相対URIが通ってしまった（偽APIの再現が甘い）"
    sent_uris = [a["action"]["uri"] for a in line_api.calls[0]["body"]["messages"][0]
                 ["quickReply"]["items"] if a["action"]["type"] == "uri"]
    assert sent_uris and all(u.startswith("/") for u in sent_uris)


# ============================================================
# 3. 送信失敗時の扱い（どの経路が退避されるか）
# ============================================================

@pytest.mark.parametrize("status", [400, 401, 429, 500])
@pytest.mark.parametrize("fn", ["send_line_message", "send_line_with_quick_reply",
                                "reply_line_message"])
def test_HTTPエラーはどの経路でもoutboxに退避されない(line_api, outbox, status, fn):
    """**退避されるのはネット断だけ**、ということの固定。

    `_enqueue_outbox` は `ConnectionError` / `Timeout` の except 節にしか無い。
    HTTP で返ってくる失敗（429 の送信枠超過、500 の LINE 側障害、
    401 のトークン失効）は False を返すだけで、その通知は永久に消える。

    本来: 429 と 5xx は一時的失敗なので outbox に積んで再送すべき
    （400/401 は積んでも無駄なので捨てて良い）。直したら
    429/500 のケースだけ `assert _read_outbox(outbox)` に書き換える。
    """
    line_api.force_status = status
    if fn == "send_line_message":
        ok = notifier.send_line_message("テスト通知", user_id=ADMIN)
    elif fn == "send_line_with_quick_reply":
        ok = notifier.send_line_with_quick_reply(
            "テスト通知", [{"label": "了解", "data": "confirm:x:y"}], user_id=ADMIN)
    else:
        ok = notifier.reply_line_message("test-reply-token-1", "テスト返信")

    assert ok is False
    assert _read_outbox(outbox) == [], "退避されるようになった（改善）"


def test_送信枠超過の429は退避されずその場で捨てられる(line_api, outbox):
    """429 は「あとで送れば届く」失敗なのに、現状は即捨て。

    通知が集中した数分だけ全滅する（＝ときどき無反応）という形で出る。
    """
    line_api.force_status = 429
    assert notifier.broadcast_line_message("食事を検知しました") == 0
    assert _read_outbox(outbox) == []


def test_返信はネット断でも退避されない(line_api, outbox):
    """reply は outbox に積まれない。

    reply token は約1分で失効するので、積んでも再送時には死んでいる。
    つまり**返信は構造的に retry 不能**で、ネットが一瞬切れた瞬間のコマンドは
    必ず無反応になる。push に切り替えるしか救いようがない。
    """
    line_api.raise_exc = requests.exceptions.ConnectionError("network down")
    assert notifier.reply_line_message("test-reply-token-1", "テスト返信") is False
    assert _read_outbox(outbox) == []


def test_ネット断のbroadcastは宛先全員分が退避される(line_api, outbox, seeded_db):
    """push 経路はネット断なら全員分積まれる（取りこぼしなし）。"""
    conn = sqlite3.connect(seeded_db)
    conn.execute("INSERT INTO family_line_users(line_user_id, person_id) VALUES(?, ?)",
                 (FAMILY_B, 3))
    conn.commit()
    conn.close()

    line_api.raise_exc = requests.exceptions.ConnectionError("network down")
    assert notifier.broadcast_line_message("浴室で30分反応がありません") == 0

    queued = _read_outbox(outbox)
    assert [e["to"] for e in queued] == [ADMIN, FAMILY_A, FAMILY_B]
    assert all(e["retry_count"] == 0 for e in queued)


def test_一部の宛先が失敗しても残りには送られる(line_api, outbox, seeded_db):
    """1人がブロックしていても他の家族には届くこと。

    ここが早期 return になると、1人の事情で家族全員が通知を失う。
    """
    line_api.fail_for = {FAMILY_A}
    sent = notifier.broadcast_line_message("テスト通知")
    assert sent == 1
    assert line_api.pushed_to == [ADMIN]
    assert len(line_api.calls) == 2, "失敗した宛先の次で止まっている"


def test_タイムアウトは退避されるがHTTPエラーは退避されない差(line_api, outbox):
    """同じ「届かなかった」なのに扱いが分かれることの対比。"""
    line_api.raise_exc = requests.exceptions.Timeout("timed out")
    notifier.send_line_message("タイムアウト分", user_id=ADMIN)
    assert len(_read_outbox(outbox)) == 1

    line_api.raise_exc = None
    line_api.force_status = 500
    notifier.send_line_message("HTTP500分", user_id=ADMIN)
    assert len(_read_outbox(outbox)) == 1, "500 も積まれるようになった（改善）"


def test_退避先は実行時のCWD依存で再送スクリプトの見る場所と一致しない(line_api, tmp_path, monkeypatch):
    """`_enqueue_outbox` は相対パス `data/line_outbox.jsonl` に書く。

    再送側は `ROOT/data/line_outbox.jsonl` を絶対パスで見るので、
    CWD が ROOT 以外のプロセスが積んだ分は**誰にも再送されない**。
    現状は systemd の WorkingDirectory と cron の `cd` で辛うじて一致しているだけで、
    手で `python scripts/...` を別ディレクトリから叩いた分は宙に浮く。

    本来: `_enqueue_outbox` も `Path(__file__).parent.parent / "data" / ...` を使う。
    """
    monkeypatch.chdir(tmp_path)
    line_api.raise_exc = requests.exceptions.ConnectionError("network down")
    notifier.send_line_message("テスト通知", user_id=ADMIN)

    stranded = tmp_path / "data" / "line_outbox.jsonl"
    assert stranded.exists(), "相対パスで書かれていない（改善）"
    assert retry_outbox.OUTBOX == ROOT / "data" / "line_outbox.jsonl"
    assert stranded != retry_outbox.OUTBOX


# ============================================================
# 4. outbox の再送（scripts/retry_line_outbox.py）
# ============================================================

@pytest.fixture
def retry_box(tmp_path, monkeypatch):
    """再送スクリプトを一時ファイルと偽 API に向ける。

    `main()` は `load_dotenv(ROOT/".env")` で**本番のトークンを環境変数に載せる**ので
    必ず潰す。`requests` も別 import なので notifier とは別に差し替える。
    """
    box = tmp_path / "line_outbox.jsonl"
    monkeypatch.setattr(retry_outbox, "OUTBOX", box)
    monkeypatch.setattr(retry_outbox, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", FAKE_TOKEN)
    api = _FakeLineAPI()
    monkeypatch.setattr(retry_outbox.requests, "post", api.post)
    api.box = box
    return api


def _entry(to: str = ADMIN, *, text: str = "退避された通知",
           queued_at: str | None = None, retry_count: int = 0) -> str:
    """outbox の1行を作る。

    **queued_at を固定日付にしてはいけない。** retry_line_outbox は
    MAX_AGE_HOURS=24 の TTL を持つので、固定日付のままだと翌日以降
    「期限切れで破棄」の経路に入り、再送の挙動を検証できなくなる。
    実際に 2026-10-19 の実行で2件が落ちた（entry が書かれた 10-15 から4日経過）。
    """
    if queued_at is None:
        queued_at = datetime.now().replace(microsecond=0).isoformat()
    return json.dumps({
        "queued_at": queued_at, "to": to, "retry_count": retry_count,
        "messages": [{"type": "text", "text": text}],
    }, ensure_ascii=False)


def test_壊れたJSON行は捨てられ残りは再送される(retry_box):
    """JSON として読めない行は読み飛ばし＋書き戻しで消える。

    捨てる判断自体は妥当（直せないので）。ただしログには出ないので、
    何通消えたかは分からない。
    """
    retry_box.box.write_text("\n".join([
        _entry(ADMIN, text="1通目"), "{壊れた行", _entry(FAMILY_A, text="3通目"),
    ]) + "\n")

    assert retry_outbox.main() == 0
    assert retry_box.texts == ["1通目", "3通目"]
    assert not retry_box.box.exists()


def test_toキーが無い行で再送が永久に止まり手前の行が毎分再送される(retry_box):
    """**outbox の最大の穴。** JSON としては読めるが `to` が無い行で KeyError。

    `entry["to"]` に素のキー参照を使っているため main() が落ち、
    ファイルの書き戻し（`write_text`）に到達しない。その結果:
      - その行より後ろの通知は**永久に送られない**
      - 手前の行は毎分 push され続ける（＝家族に同じ通知が毎分届く）
      - retry_count も TTL も進まないので、自然に止まらない

    `to` が無い行は、notifier 側の仕様変更やファイル破損（電源断で途中まで
    書かれた行）で現実に発生しうる。

    本来: 行単位で try/except し、不正な行は dropped として捨てる。
    直したら「1通目と3通目が送られ、ファイルが消える」に書き換える。
    """
    bad = json.dumps({"queued_at": datetime.now().replace(microsecond=0).isoformat(),
                      "messages": [{"type": "text", "text": "宛先なし"}]})
    retry_box.box.write_text("\n".join([
        _entry(ADMIN, text="1通目"), bad, _entry(FAMILY_A, text="3通目"),
    ]) + "\n")

    for _ in range(3):   # cron 3回ぶん
        with pytest.raises(KeyError):
            retry_outbox.main()

    assert retry_box.texts == ["1通目", "1通目", "1通目"], "毎分の重複が止まっている（改善）"
    assert len(retry_box.box.read_text().splitlines()) == 3, "書き戻しが起きている（改善）"
    assert all(json.loads(l).get("retry_count", 0) == 0
               for l in retry_box.box.read_text().splitlines() if l.strip())


def test_同じ内容の行は重複排除されず全部送られる(retry_box):
    """同一メッセージが3行積まれていれば3通届く。

    `_enqueue_outbox` は重複チェックをしないので、同じ通知が
    リトライのたびに積み増されると家族には同じ文面が複数届く。
    """
    retry_box.box.write_text("\n".join([_entry(ADMIN, text="同じ通知")] * 3) + "\n")
    retry_outbox.main()
    assert retry_box.texts == ["同じ通知"] * 3


def test_再送中に積まれた行は書き戻しで消える(retry_box):
    """**送信中に web/monitor が積んだ通知が消える競合。**

    `main()` は `read_text()` → 送信 → `write_text()`（全文上書き）/ `unlink()` で、
    読んだあとに追記された行を考慮しない。毎分走る cron と、いつでも通知を積む
    web/monitor が同じファイルを触るので、取り逃しは時間の問題。
    送信は1通あたり最大10秒待つので窓は十分に広い。

    本来: 読み込みと同時にファイルを rename して自分専用にする（または O_APPEND と
    行単位のロックを使う）。
    """
    late = _entry(FAMILY_A, text="再送中に積まれた通知")

    def append_during_send():
        with retry_box.box.open("a") as f:    # 別プロセスからの追記を模擬
            f.write(late + "\n")

    retry_box.box.write_text(_entry(ADMIN, text="先にあった通知") + "\n")
    retry_box.on_call = append_during_send
    retry_outbox.main()

    assert retry_box.texts == ["先にあった通知"]
    assert not retry_box.box.exists(), "追記分が残っている（改善）"


def test_TTL超過の行は破棄される(retry_box):
    """24時間以上古い行は諦めて捨てる（無限増加の防止）。"""
    from datetime import timedelta
    old_ts = (datetime.now() - timedelta(hours=48)).replace(microsecond=0).isoformat()
    retry_box.box.write_text("\n".join([
        _entry(ADMIN, text="古い", queued_at=old_ts),   # TTL 24時間を超過
        _entry(FAMILY_A, text="新しい"),                 # 今
    ]) + "\n")
    retry_outbox.main()
    assert retry_box.texts == ["新しい"]


def test_再送回数の上限を超えた行は破棄される(retry_box):
    """retry_count>=50 で破棄。ここが効くので通常は無限ループしない。"""
    retry_box.box.write_text(_entry(ADMIN, retry_count=50) + "\n")
    retry_outbox.main()
    assert retry_box.calls == []
    assert not retry_box.box.exists()


def test_恒久的な400も50回ぶん再送し続ける(retry_box):
    """400（宛先が不正・本文が長すぎ）は何回送っても通らないのに同じ扱い。

    毎分1回、50分ぶん無駄に叩き続ける。送信枠と429のリスクを増やすだけ。

    本来: 4xx（429 以外）は即破棄し、429/5xx だけ再送する。
    """
    retry_box.force_status = 400
    retry_box.box.write_text(_entry(ADMIN) + "\n")
    retry_outbox.main()

    left = _read_outbox(retry_box.box)
    assert len(left) == 1
    assert left[0]["retry_count"] == 1
    assert "HTTP 400" in left[0]["last_error"]


def test_5000文字超の巨大な行も切られずに投げ直される(retry_box):
    """長すぎる本文はどこでも切り詰められない。

    LINE は 5000 文字超を 400 で拒否するので、この行は 50 回失敗して消えるだけ。
    家族には最初から最後まで何も届かない。
    """
    retry_box.box.write_text(_entry(ADMIN, text="あ" * 6000) + "\n")
    retry_outbox.main()

    assert retry_box.deliveries == []
    left = _read_outbox(retry_box.box)
    assert len(left[0]["messages"][0]["text"]) == 6000, "どこかで切り詰めている（改善）"


def test_トークン未設定なら何も送らずに終わる(retry_box, monkeypatch):
    """トークンが読めないときに空の Bearer で叩かないこと。"""
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    retry_box.box.write_text(_entry(ADMIN) + "\n")
    assert retry_outbox.main() == 1
    assert retry_box.calls == []
    assert retry_box.box.exists(), "送れないのに退避ファイルを消している"


# ============================================================
# 5. 宛先の決まり方
# ============================================================

def test_宛先未指定のsend_line_messageはadminにしか届かない(line_api, seeded_db):
    """`send_line_message(msg)` は孫1人への通知。意図通りだが取り違えやすい。

    「家族に知らせたつもり」で書くと孫だけに届く。
    `notify_device_unlocked(manual=False)` が現にこの経路。
    """
    conn = sqlite3.connect(seeded_db)
    conn.execute("INSERT INTO family_line_users(line_user_id, person_id) VALUES(?, ?)",
                 (FAMILY_B, 3))
    conn.commit()
    conn.close()

    assert notifier.send_line_message("炊飯器のロックが自動で解除されました") is True
    assert line_api.pushed_to == [ADMIN]


def test_broadcastはenvとDBの宛先を重複なく全員に送る(line_api, seeded_db):
    """.env の LINE_ALLOWED_SENDERS + LINE_USER_ID + DB の family_line_users を統合。

    admin が両方に居ても1通だけ（二重送信しない）。
    """
    conn = sqlite3.connect(seeded_db)
    conn.executemany(
        "INSERT INTO family_line_users(line_user_id, person_id) VALUES(?, ?)",
        [(FAMILY_B, 3), (ADMIN, 3)],
    )
    conn.commit()
    conn.close()

    assert notifier.broadcast_line_message("テスト通知") == 3
    assert line_api.pushed_to == [ADMIN, FAMILY_A, FAMILY_B]


def test_DBが読めなくてもenvの宛先には届く(line_api, temp_db, monkeypatch):
    """family_line_users が読めないときに通知を全滅させないこと。

    DB ロックや破損のときに「LINE も鳴らない」が重なるのが最悪なので、
    env 由来の宛先だけでも生き残る設計になっている。
    """
    import src.db as db

    def _boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "get_conn", _boom)
    assert notifier.broadcast_line_message("テスト通知") == 2
    assert line_api.pushed_to == [ADMIN, FAMILY_A]


def test_非CRITICALの確認通知はadmin1人にしか届かない(line_api, seeded_db):
    """**「送ったつもりが孫にしか届いていない」経路。**

    `send_actionable_notification` は CRITICAL 以外を admin だけに送る。
    お薬リマインダーや未確定セッションの確認は家族の誰も見ていない。
    pending には記録されるので家族UIには出るが、LINE には出ない。

    設計意図（送信枠と「家族を不安にさせない」）があるので穴ではないが、
    通知カテゴリを足すときに CRITICAL_CATEGORIES へ入れ忘れると
    「鳴らないのに鳴ったつもり」になる。
    """
    conn = sqlite3.connect(seeded_db)
    conn.execute("INSERT INTO family_line_users(line_user_id, person_id) VALUES(?, ?)",
                 (FAMILY_B, 3))
    conn.commit()
    conn.close()

    sent = notifier.send_actionable_notification(
        "medicine_reminder", "2026-10-15_morning", "お薬の時間です")
    assert sent == 1
    assert line_api.pushed_to == [ADMIN]


def test_CRITICALの確認通知は家族全員に届く(line_api, seeded_db):
    """浴室緊急などは全員に出ること（ここが admin だけになったら致命的）。"""
    conn = sqlite3.connect(seeded_db)
    conn.execute("INSERT INTO family_line_users(line_user_id, person_id) VALUES(?, ?)",
                 (FAMILY_B, 3))
    conn.commit()
    conn.close()

    sent = notifier.send_actionable_notification(
        "bath_emergency", "2026-10-15_bath_1", "浴室で30分反応がありません")
    assert sent == 3
    assert line_api.pushed_to == [ADMIN, FAMILY_A, FAMILY_B]


def test_宛先が空文字のQuickReplyはadminに送られてしまう(line_api):
    """`user_id` が空だと `env["LINE_USER_ID"]`（admin）に落ちる。

    `uid = user_id or env.get("LINE_USER_ID", "")` のため、宛先が取れなかった
    ときに「送信失敗」ではなく「孫に誤配」になる。group/room から来た
    イベントで sender_id が取れないときに踏みうる。

    本来: 宛先未指定（None）と「空文字が渡された」は区別して、後者は送らない。
    """
    ok = notifier.send_line_with_quick_reply(
        "テスト通知", [{"label": "了解", "data": "confirm:x:y"}], user_id="")
    assert ok is True
    assert line_api.pushed_to == [ADMIN], "空宛先が誤配されなくなった（改善）"


# ============================================================
# 6. Quick Reply と本文の上限
# ============================================================

def test_QuickReplyのボタンは13個までで14個目以降は黙って落ちる(line_api):
    """LINE の上限は13個。超過分は警告もなく切り捨てられる。

    切り捨て自体は 400 を避けるために必要。問題は「落ちたことが分からない」点で、
    未確定セッションの人物選択のように候補数が可変のボタンでは、
    14個目以降の選択肢が家族から見えなくなる。
    """
    items = [{"label": f"候補{i}", "data": f"attribute:S:{i}"} for i in range(1, 21)]
    assert notifier.send_line_with_quick_reply("選んでください", items, user_id=ADMIN) is True

    labels = [a["label"] for a in line_api.deliveries[0]["quick"]]
    assert len(labels) == 13
    assert labels[-1] == "候補13"
    assert "候補14" not in labels


def test_ボタンのラベルは20文字で切られる(line_api):
    """label は20文字上限。超えると 400 になるので切り詰めは正しい。"""
    long_label = "あ" * 30
    notifier.send_line_with_quick_reply(
        "テスト", [{"label": long_label, "data": "confirm:x:y"}], user_id=ADMIN)
    assert line_api.deliveries[0]["quick"][0]["label"] == "あ" * 20


def test_ボタンが0個のQuickReplyはLINEに弾かれて丸ごと消える(line_api):
    """uri も data も無い項目だけだと `items: []` になり 400。

    「ボタンを出したいが候補が0件」のときに本文ごと消える。
    本来: items が空なら Quick Reply を外して平文 push にフォールバックする。
    """
    ok = notifier.send_line_with_quick_reply(
        "確認してください", [{"label": "ラベルだけ"}], user_id=ADMIN)
    assert ok is False
    assert line_api.deliveries == []
    assert line_api.calls[0]["body"]["messages"][0]["quickReply"]["items"] == []


def test_labelが無いQuickReply項目は例外になって送信全体が落ちる(line_api):
    """`it["label"][:20]` が素のキー参照なので KeyError。

    try より手前なので notifier 内では捕まらず、呼び出し元まで飛ぶ。
    `broadcast_with_quick_reply` 経由だと、1項目の作り間違いで
    その通知だけでなく呼び出し元の処理（検知フロー）まで巻き込む。

    本来: `it.get("label", "")` にして、その項目を捨てるだけに留める。
    """
    with pytest.raises(KeyError):
        notifier.send_line_with_quick_reply(
            "テスト", [{"data": "confirm:x:y"}], user_id=ADMIN)
    assert line_api.calls == []


@pytest.mark.parametrize("fn", ["reply", "push"])
def test_5000文字を超える本文は切られずに送られて弾かれる(line_api, outbox, fn):
    """本文の長さ上限がどこにも無いことの固定。

    LINE のテキストは5000文字まで。超えると 400 で、reply なら無反応、
    push なら通知が消える。家族の「意見」転送や、記録が多い日の
    状況サマリのように**入力次第で長くなる本文が実際にある**。

    本来: 4900 文字程度で切って「…（以下省略）」を付ける。
    """
    body = "あ" * 6000
    if fn == "reply":
        ok = notifier.reply_line_message("test-reply-token-1", body)
    else:
        ok = notifier.send_line_message(body, user_id=ADMIN)

    assert ok is False
    assert line_api.deliveries == []
    assert len(line_api.calls[0]["body"]["messages"][0]["text"]) == 6000
    assert _read_outbox(outbox) == []


def test_意見の転送が失敗しても送信者には成功と返る(line_api, seeded_db):
    """「開発者に伝えました」が嘘になる経路。

    `handle_feedback` は `send_line_message` の戻り値を見ない。長文の意見
    （5000文字超）やネット断のとき、本人には成功と返るのに転送は消えている。
    DB の feedback テーブルには残るので完全な消失ではないが、誰も見ない。

    本来: 転送の成否を見て「あとで届きます」等に文面を変える。
    """
    line_api.force_status = 400
    reply = line_commands.handle_feedback("意見 " + "あ" * 6000, FAMILY_A)

    assert reply.startswith("✅")
    assert line_api.deliveries == []
    conn = sqlite3.connect(seeded_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
    finally:
        conn.close()


# ============================================================
# 7. 返信が1通も出ない入力（黙って落ちる経路）
# ============================================================

def test_コマンドに当たらない文には何も返さない(webhook, line_api):
    """`dispatch` が None を返し URL 要求でもない文は、送信そのものが起きない。

    2026-10-15 まで完全に無言で、打ち間違いも言い換えも同じ黙り方だった。
    実測で 272 パターン中 93 がここに落ちており、家族は正しい言い方を
    学ぶ手がかりが無かった。今は案内を返す。
    """
    resp = webhook(_line_body("ありがとう"))
    assert resp.status_code == 200
    assert line_api.calls, "未知の入力にも案内を返すこと"
    body = str(line_api.calls[-1])
    assert "ヘルプ" in body, "正しい言い方への手がかりを含めること"


@pytest.mark.parametrize("data", [
    "unknown_action",        # 知らないボタン（古い通知のボタンを後から押した等）
    "confirm:",              # 形式崩れ（parts が3つに割れない）
    "attribute:S:1",         # session_id が数値でない
    "merge:1",               # 要素不足
])
def test_解釈できないpostbackは何も返さない(webhook, line_api, data):
    """ボタンを押したのに完全に無反応になる経路。

    `handle_*_postback` は形式が合わなければ None を返し、webhook は
    `if reply:` で送信を省く。家族から見ると「ボタンが効かない」。
    古い通知のボタン（アプリに残り続ける）を押したときに踏みうる。

    本来: 解釈できない postback にも「この操作はもう使えません」と返すべき。
    """
    resp = webhook(_postback_body(data))
    assert resp.status_code == 200
    assert line_api.calls == [], f"{data} で送信が起きた（改善）"


def test_postback処理が例外を出したら警告文が返る(webhook, line_api, monkeypatch):
    """ハンドラが落ちたときだけは利用者に見える形で返る（無言ではない）。

    webhook は `from ..line_commands import ...` をリクエストごとに実行するので、
    モジュール属性の差し替えが効く。
    """
    async def _boom(data, sender_id):
        raise RuntimeError("テスト用の故障")

    monkeypatch.setattr(line_commands, "handle_confirm_postback", _boom)
    webhook(_postback_body("confirm:meal_alert:2026-10-15_1"))
    assert line_api.texts == ["⚠️ 処理に失敗しました"]


def test_対象の通知が無いボタンは偽の共有通知を出さない(webhook, line_api):
    """古い通知のボタンを押したときの挙動。

    2026-10-15 まで、対象の pending が1行も無くても
    「☑️ 対応しました」が家族全員に broadcast され、押した本人には
    「家族全員に共有しました」と返っていた。存在しない context_key を
    送るだけで偽の完了通知を作れる状態だった。

    ただし broadcast の成功数を戻り値に含めていない点（全員の push が
    失敗しても「共有しました」と返る）は未修正のまま残っている。
    """
    line_api.fail_for = {ADMIN, FAMILY_A, FAMILY_B}   # 全員の push が 400
    webhook(_postback_body("confirm:meal_alert:存在しないctx"))

    assert not any("家族全員に共有しました" in t for t in line_api.texts), \
        "対象が無いのに共有したと言わないこと"
    assert line_api.attempted_apis.count("push") == 0, \
        "broadcast そのものを試みないこと"
