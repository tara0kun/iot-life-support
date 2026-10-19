"""公開インターネットに出ている FastAPI 50 エンドポイントの認可。

このアプリは Tailscale Funnel でパブリック公開されていて、認証の向こう側には
祖母の居室のライブカメラ・生活パターン・炊飯器の電源操作がある。
認可が1箇所外れるだけで、第三者が祖母の生活を覗けるか、炊飯器を触れてしまう。

このファイルが守っている、実際に起きた事故:

  - 2026-10-15 `_check_tablet_access` が、TABLET_TOKEN を読めないときに `return True`
    していた（commit 91e35fb で fail-closed 化）。トークンが読めなくなった瞬間に
    外部の誰でも /tablet を開けた。
  - 2026-10-15 家族マニュアルの画面キャプチャが `/static/screenshots/` に置かれ、
    無認証で配信されていた（外部から14回取得されている）。画像には祖母の食事時刻・
    達成項目・LINE通知の本文が写っており、ファイル名は public な README 経由で既知だった。
  - 2026-10-15 webhook の許可リスト判定が `startswith("登録")` で、未登録の送信者が
    「登録一覧」を送ると家族の氏名一覧が返ってきていた（commit f569db5 で厳格化）。
  - 2026-09-26 食事写真が無認証の公開URLで配信されていた。ファイル名が
    `{session_id}_{UNIX秒}.jpg` で列挙でき、家族が「削除」した写真まで取得できた。

方針は「通ったら安心」ではなく「壊れたら気づける」。
中心は ROUTE_POLICY 表で、エンドポイントが1つ増えたら
`test_全エンドポイントが認可表に載っていること` が落ちる。認可クラスを決めない限り
マージできない、というのがこのファイルの本当の仕事。

テストデータは全部架空。実在の個人名・LINE user ID・トークン・パスワードは書かない。
本番の `data/meal_photos/` と `data/manual_images/` には実物が入っているので、
fixture で必ず一時ディレクトリに差し替える（本番の画像をテストで配信しない）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import time
import types

import pytest
from fastapi.testclient import TestClient

# --- 架空のテスト用資格情報。実物とは一切関係ない ---------------------------
FAMILY_PASSWORD = "test-family-password-not-real"
TABLET_TOKEN = "test-tablet-token-not-real"
LINE_SECRET = "test-line-channel-secret-not-real"
SESSION_SECRET = "0" * 64
# LINE の user ID は U + 32桁hex の形だけ真似た架空値
REGISTERED_SENDER = "U" + "a" * 32   # 登録済みの「テスト家族」
STRANGER_SENDER = "U" + "b" * 32     # 未登録の第三者

LAN_HOST = "192.168.1.50"            # 宅内 LAN の祖母タブレットを模擬
EXTERNAL_HOST = "203.0.113.9"        # ドキュメント用の外部 IP (RFC 5737)


# ============================================================
# 認可表: 誰が到達できるべきか
# ============================================================
PUBLIC = "public"           # 未認証で誰でも到達してよい（ログイン画面・ガイド等）
FAMILY = "family"           # 家族セッション必須
TABLET = "tablet"           # 宅内 LAN または TABLET_TOKEN
SIGNED = "signed"           # 独自の署名検証を持つ（個別テストで検証）
KNOWN_OPEN = "known_open"   # **現状、認可が無い。既知の穴。下の characterization テスト参照**

ROUTE_POLICY: dict[tuple[str, str], str] = {
    # 公開してよいもの
    ("GET", "/"): PUBLIC,                      # /tablet への 303 のみ
    ("GET", "/family/login"): PUBLIC,
    ("POST", "/family/login"): PUBLIC,         # ただしブルートフォース制限あり
    ("GET", "/family/logout"): PUBLIC,
    ("GET", "/guide"): PUBLIC,
    ("GET", "/guide/"): PUBLIC,
    ("GET", "/guide/{slug}"): PUBLIC,          # 家族が LINE のリンクから開く手順書
    ("GET", "/line/webhook"): PUBLIC,          # LINE コンソールの疎通確認
    # 独自の署名検証
    ("POST", "/line/webhook"): SIGNED,         # x-line-signature (HMAC-SHA256)
    ("GET", "/photos/{file_name}"): SIGNED,    # family / tablet / 期限付き署名URL の3経路
    # 祖母のタブレット（宅内 or トークン）
    ("GET", "/tablet"): TABLET,
    ("POST", "/api/tablet-record"): TABLET,
    ("GET", "/api/meal-photo-prompt"): TABLET,
    ("POST", "/api/meal-photo"): TABLET,
    # 家族セッション必須
    ("GET", "/api/events"): FAMILY,
    ("GET", "/api/sessions/{person_id}"): FAMILY,
    ("GET", "/api/persons"): FAMILY,
    ("GET", "/api/camera/snapshot"): FAMILY,
    ("GET", "/api/camera/mjpeg"): FAMILY,
    ("GET", "/api/device-status"): FAMILY,
    ("POST", "/api/unlock"): FAMILY,
    ("POST", "/api/lock"): FAMILY,
    ("GET", "/api/meal-photos"): FAMILY,
    ("DELETE", "/api/meal-photos/{photo_id}"): FAMILY,
    ("POST", "/api/quick-record"): FAMILY,
    ("POST", "/api/family-prompt"): FAMILY,
    ("GET", "/family/manual"): FAMILY,
    ("GET", "/family/manual/image/{file_name}"): FAMILY,
    ("GET", "/family/weekly-report"): FAMILY,
    ("GET", "/api/face-candidates"): FAMILY,
    ("GET", "/face-candidates/{file_name}"): FAMILY,
    ("POST", "/api/face-candidates/label"): FAMILY,
    ("POST", "/api/face-candidates/dismiss"): FAMILY,
    ("GET", "/family/face-learning"): FAMILY,
    ("GET", "/api/pending-notifications"): FAMILY,
    ("POST", "/api/pending-notifications/{notif_id}/respond"): FAMILY,
    ("GET", "/api/learning-stats"): FAMILY,
    ("GET", "/api/heatmap"): FAMILY,
    ("GET", "/api/settings"): FAMILY,
    ("POST", "/api/settings"): FAMILY,
    ("GET", "/api/medicine-schedule"): FAMILY,
    ("POST", "/api/medicine-schedule"): FAMILY,
    ("DELETE", "/api/medicine-schedule/{timing}"): FAMILY,
    ("POST", "/api/rice-guide"): FAMILY,
    ("DELETE", "/api/rice-guide"): FAMILY,
    ("POST", "/api/events/{event_id}/edit"): FAMILY,
    ("DELETE", "/api/events/{event_id}"): FAMILY,
    ("GET", "/family"): FAMILY,
    # 現状、認可が無い（既知の穴。実装は直さずテストで固定している）
    ("POST", "/api/dismiss-prompt/{prompt_id}"): KNOWN_OPEN,
    ("WEBSOCKET", "/ws/events"): KNOWN_OPEN,
}

PATH_ARGS = {
    "{person_id}": "1",
    "{photo_id}": "1",
    "{notif_id}": "1",
    "{event_id}": "1",
    "{prompt_id}": "1",
    "{timing}": "test",
    "{file_name}": "does-not-exist-test.jpg",
    "{slug}": "tablet-setup",
}


def _concrete(path: str) -> str:
    """認可表のパステンプレートを、実際に投げられる URL にする。"""
    for placeholder, value in PATH_ARGS.items():
        path = path.replace(placeholder, value)
    assert "{" not in path, f"PATH_ARGS に未定義のパスパラメータがある: {path}"
    return path


def _family_routes() -> list[tuple[str, str]]:
    return sorted(k for k, v in ROUTE_POLICY.items() if v == FAMILY)


# ============================================================
# 足場
# ============================================================

def _camera_must_not_start(*args, **kwargs):
    """未認証リクエストでカメラが起動したら、その場で落とす。

    認可チェックより先にカメラが回ると、祖母の居室の映像フレームが
    リクエスト側に渡る前に生成されてしまう。順序も含めて守りたい。
    """
    raise AssertionError("認可より先にカメラストリーマが起動した")


@pytest.fixture
def web(temp_db, tmp_path, monkeypatch):
    """認可だけを見るために、外部依存を全部差し替えた app を返す。

    差し替える理由:
      - `_load_*` は `.env` を読む（本番の値をテストに持ち込まない）
      - MEAL_PHOTOS_DIR / MANUAL_IMAGES_DIR は本番の実画像が入っている
      - notifier / lock_manager は**実際に LINE を送り Matter を叩く**。
        `/api/tablet-record` は送信失敗を `except Exception: pass` で飲むので、
        差し替えないとテストが家族の LINE に通知を飛ばす
    """
    import src.web.app as app_module
    import src.notifier as notifier

    monkeypatch.setattr(
        app_module, "_load_family_password",
        lambda: hashlib.sha256(FAMILY_PASSWORD.encode()).hexdigest(),
    )
    monkeypatch.setattr(app_module, "_load_tablet_token", lambda: TABLET_TOKEN)
    monkeypatch.setattr(app_module, "_load_line_secret", lambda: LINE_SECRET)
    monkeypatch.setattr(app_module, "_load_line_allowed_senders", lambda: {REGISTERED_SENDER})
    monkeypatch.setattr(app_module, "_session_secret", lambda: SESSION_SECRET)

    photos = tmp_path / "meal_photos"
    photos.mkdir()
    manual = tmp_path / "manual_images"
    manual.mkdir()
    monkeypatch.setattr(app_module, "MEAL_PHOTOS_DIR", photos)
    monkeypatch.setattr(app_module, "MANUAL_IMAGES_DIR", manual)

    # ブルートフォース制限はプロセス内メモリに状態を持ち、テスト間で持ち越す。
    # 既定では無効にして、制限そのものを見るテストだけ個別に有効化する。
    monkeypatch.setattr(app_module.limiter, "enabled", False)

    # 外界に出る副作用はここに記録する。未認証リクエストで1件でも増えたら認可が壊れている。
    effects: list[str] = []
    for name in ("send_line_message", "send_actionable_notification",
                 "reply_line_message", "send_line_with_quick_reply",
                 "notify_device_locked", "notify_device_unlocked"):
        monkeypatch.setattr(
            notifier, name,
            lambda *a, _n=name, **k: effects.append(f"line:{_n}"),
        )

    async def _fake_lock(device_name, node_id, reason=""):
        effects.append("matter:lock")
        return False

    async def _fake_unlock(device_name, node_id, reason=""):
        effects.append("matter:unlock")
        return False

    monkeypatch.setattr(app_module, "lock_device", _fake_lock)
    monkeypatch.setattr(app_module, "unlock_device", _fake_unlock)
    monkeypatch.setattr(app_module, "get_streamer", _camera_must_not_start)

    return types.SimpleNamespace(
        module=app_module, app=app_module.app, db_path=temp_db,
        effects=effects, photos_dir=photos, manual_dir=manual,
    )


def _make_client(web, host: str = EXTERNAL_HOST) -> TestClient:
    """指定の送信元 IP を持つクライアント。

    TestClient の既定の client.host は "testclient" で、`_is_local()` は False を返す。
    つまり既定でも「外部クライアント」の模擬になっているが、宅内/外部の切り替えを
    明示したいので毎回 IP を渡す。
    """
    return TestClient(web.app, client=(host, 50000), follow_redirects=False)


@pytest.fixture
def client(web):
    """外部インターネットからの未認証クライアント。"""
    with _make_client(web) as c:
        yield c


@pytest.fixture
def family_client(web):
    """外部からログイン済みの家族クライアント。"""
    with _make_client(web) as c:
        r = c.post("/family/login", data={"password": FAMILY_PASSWORD})
        assert r.status_code == 303, "テストのログイン手順が壊れている"
        yield c


@pytest.fixture
def lan_client(web):
    """宅内 LAN のクライアント（祖母のタブレット相当）。"""
    with _make_client(web, LAN_HOST) as c:
        yield c


def _denied(resp) -> bool:
    """「到達できなかった」と言える応答か。

    401/403 のほか、画面系は /family/login への 303 で弾く実装になっている。
    """
    if resp.status_code in (401, 403):
        return True
    if resp.status_code == 303:
        return resp.headers.get("location", "").startswith("/family/login")
    return False


# ============================================================
# 1. 認可表そのもの
# ============================================================

def test_全エンドポイントが認可表に載っていること(web):
    """**このファイルで一番大事なテスト。**

    エンドポイントは50個ある。新しく1つ足したとき、認可を書き忘れても
    誰も気づかない。実際に、家族マニュアルの画像配信は
    `/static/screenshots/` に置かれたまま3週間気づかれず、外部から14回取得された。

    追加/削除したら、まず ROUTE_POLICY に認可クラスを書くこと。
    「公開でよい」と判断したなら PUBLIC と書けばこのテストは通る。
    判断そのものを強制するのが目的。
    """
    from fastapi.routing import APIRoute, APIWebSocketRoute

    actual: set[tuple[str, str]] = set()
    for route in web.app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                if method in ("HEAD", "OPTIONS"):
                    continue
                actual.add((method, route.path))
        elif isinstance(route, APIWebSocketRoute):
            actual.add(("WEBSOCKET", route.path))

    declared = set(ROUTE_POLICY)
    missing = sorted(actual - declared)
    stale = sorted(declared - actual)
    assert not missing, (
        "認可表に無いエンドポイントがある。誰が到達してよいか決めて ROUTE_POLICY に足すこと:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in missing)
    )
    assert not stale, (
        "認可表にあるのに実装に無い（パスの綴り間違い？表が形骸化している）:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in stale)
    )


# ============================================================
# 2. 家族ダッシュボード: 未認証を弾く
# ============================================================

@pytest.mark.parametrize(("method", "path"), _family_routes(), ids=lambda v: str(v))
def test_家族エンドポイントは未認証を弾く(client, method, path):
    """家族専用の34エンドポイント全部に、未認証の外部クライアントで到達を試みる。

    ここが1つでも通ると、祖母の生活ログ・居室のカメラ・顔画像・編集削除が
    公開インターネットから触れる。DELETE 系（イベント削除・写真削除・
    薬スケジュール削除）は取り返しがつかないので特に重要。

    認可チェックは必ずハンドラの先頭にあること（本文のパースより前）。
    body 無しで投げているので、認可より先に body を読む実装になると
    422/500 になってこのテストが落ちる。
    """
    resp = client.request(method, _concrete(path))
    assert _denied(resp), (
        f"{method} {path} に未認証で到達できた: status={resp.status_code}"
    )


def test_家族セッションがあれば家族エンドポイントに到達できる(family_client):
    """前のテストが「URLを間違えていたから401」ではないことを示す。

    参照系だけを叩く（更新系を認証付きで叩くと本当に DB を書くので入れない）。
    """
    for path in ("/api/events", "/api/persons", "/api/device-status",
                 "/api/settings", "/api/pending-notifications", "/api/meal-photos"):
        resp = family_client.get(path)
        assert resp.status_code == 200, f"{path} が家族セッションで開けない: {resp.status_code}"
    assert family_client.get("/family").status_code == 200
    assert family_client.get("/family/manual").status_code == 200


def test_未認証リクエストは外部に副作用を出さない(client, web):
    """未認証で炊飯器の電源操作や LINE 送信が走らないこと。

    `/api/unlock` `/api/lock` は Matter で実際に祖母宅のプラグを操作し、
    `/api/family-prompt` は祖母のタブレットに表示を出し、
    `/api/tablet-record` は家族の LINE に通知を飛ばす。
    401 を返すだけでなく、**その前に何も起きていない**ことを見る。
    認可チェックがハンドラの後ろに移動すると落ちる。
    """
    client.post("/api/unlock", json={"device": "rice_cooker"})
    client.post("/api/lock", json={"device": "rice_cooker"})
    client.post("/api/family-prompt", json={"message": "テスト", "minutes": 1})
    client.post("/api/tablet-record", json={"activity": "お薬", "person_id": 1})
    client.post("/api/quick-record", json={"activity": "お薬", "person_id": 1})
    assert web.effects == [], f"未認証リクエストで外部に副作用が出た: {web.effects}"


def test_カメラは未認証では1フレームも作らない(client):
    """祖母の居室のライブカメラ。認可より先にストリーマを起動しないこと。

    fixture が `get_streamer` を「呼ばれたら落ちる」関数に差し替えてあるので、
    認可チェックが後ろに移動すると 401 ではなく AssertionError で落ちる。
    """
    assert client.get("/api/camera/snapshot").status_code == 401
    assert client.get("/api/camera/mjpeg").status_code == 401


def test_署名の無いセッションCookieでは家族になれない(client):
    """SessionMiddleware の署名が外れていないこと。

    `{"family_auth": true}` を base64 にしただけの Cookie を送る。
    署名付き Cookie を使っている限りこれは通らない。
    署名なし Cookie 実装に変わると、4桁PINを破らなくても家族になれる。
    """
    forged = base64.b64encode(json.dumps({"family_auth": True}).encode()).decode()
    client.cookies.set("session", forged)
    assert client.get("/api/events").status_code == 401
    assert _denied(client.get("/family"))


def test_タブレットセッションは家族の権限にならない(web, lan_client):
    """権限の分離。祖母のタブレットから家族UIが開けてはいけない。

    タブレットは祖母の居室に置きっぱなしで、来訪者・介護者も手に取れる。
    そこから家族の氏名・LINE通知本文・イベント編集削除に届くと、
    「家族だけが見る」前提が崩れる。
    """
    assert lan_client.get("/tablet").status_code == 200, "宅内タブレットが開けない"
    assert lan_client.get("/api/events").status_code == 401
    assert _denied(lan_client.get("/family"))
    assert lan_client.get("/api/settings").status_code == 401
    assert lan_client.delete("/api/events/1").status_code == 401


def test_ログアウトでセッションが無効になる(family_client):
    """家族が共用端末でログアウトしたあと、戻るボタンで再び見えてはいけない。"""
    assert family_client.get("/api/events").status_code == 200
    family_client.get("/family/logout")
    assert family_client.get("/api/events").status_code == 401


# ============================================================
# 3. タブレットアクセスは fail-closed
# ============================================================

def _fake_request(host: str, query: dict | None = None, session: dict | None = None):
    """`_check_tablet_access` を直接呼ぶための最小の Request 代用。"""
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=host),
        query_params=query or {},
        session=session if session is not None else {},
    )


def test_トークンが読めないとき外部クライアントは弾かれる_fail_closed(web, monkeypatch):
    """2026-10-15 まで、ここは `return True` だった（commit 91e35fb）。

    TABLET_TOKEN が `.env` から読めなくなった瞬間（タイポ・権限事故・
    .env の入れ替え）に、Tailscale Funnel 経由で**誰でも**祖母の見守り画面を
    開ける状態になっていた。画面には食事時刻・入浴・服薬・家族からの
    メッセージがそのまま出る。

    fail-open に戻すと落ちる。
    """
    import src.web.app as app_module

    assert app_module._load_tablet_token() == TABLET_TOKEN
    # .env が読めない/空の状況を作る
    monkeypatch.setattr(app_module, "_load_tablet_token", lambda: "")

    external = _fake_request(EXTERNAL_HOST)
    assert app_module._check_tablet_access(external) is False, (
        "トークンが空のとき外部クライアントを通してしまった（fail-open）"
    )
    # 宅内はトークンに依存せず通る。これが揃って初めて fail-closed が安全になる
    assert app_module._check_tablet_access(_fake_request(LAN_HOST)) is True
    assert app_module._check_tablet_access(_fake_request("127.0.0.1")) is True


def test_トークンが読めないとき外部からタブレット画面が開けない(web, monkeypatch):
    """上の単体テストと同じことを HTTP 経路で確認する。

    ハンドラが `_check_tablet_access` の戻り値を無視して描画してしまう
    （403 を返し忘れる）回帰もここで止まる。
    """
    monkeypatch.setattr(web.module, "_load_tablet_token", lambda: "")
    with _make_client(web) as c:
        resp = c.get("/tablet")
        assert resp.status_code == 403
        assert "起床" not in resp.text, "403 なのに画面の中身が漏れている"
    # 宅内の祖母タブレットは影響を受けない
    with _make_client(web, LAN_HOST) as c:
        assert c.get("/tablet").status_code == 200


def test_外部からはトークンが合ったときだけタブレットが開ける(web):
    """トークン判定が「空でなければ通す」等に緩まると落ちる。"""
    with _make_client(web) as c:
        assert c.get("/tablet").status_code == 403, "トークン無しで開けた"
        assert c.get("/tablet", params={"token": "wrong-token"}).status_code == 403
        assert c.get("/tablet", params={"token": TABLET_TOKEN}).status_code == 200
        # 一度通ったらセッションで維持される（タブレットは毎回URLを入れ直せない）
        assert c.get("/tablet").status_code == 200


def test_タブレット専用APIも同じ門をくぐる(web):
    """祖母の記録 API が外部から叩けると、記録が捏造され家族に誤通知が飛ぶ。

    2026-09 に「入浴が間食に化けて祖母に誤情報を出した」事故があり、
    記録の出どころは信用の起点になっている。
    """
    with _make_client(web) as c:
        assert c.post("/api/tablet-record", json={"activity": "お薬"}).status_code == 403
        assert c.get("/api/meal-photo-prompt").status_code == 403
        files = {"photo": ("x.jpg", b"\xff\xd8test", "image/jpeg")}
        resp = c.post("/api/meal-photo", files=files, data={"session_id": "1"})
        assert resp.status_code == 403
    with _make_client(web, LAN_HOST) as c:
        assert c.get("/api/meal-photo-prompt").status_code == 200


# ============================================================
# 4. 家族マニュアルの画像
# ============================================================

def test_家族マニュアル画像は家族認証が無いと配信されない(web, client, family_client):
    """2026-10-15 まで `/static/screenshots/` から無認証で配信されていた。

    7枚とも実機の画面で、祖母の食事時刻・達成項目・LINE通知の本文が写っている。
    ファイル名は public リポジトリの README 経由で既知だったため、
    外部から14回取得されている。
    """
    (web.manual_dir / "test-manual-shot.jpg").write_bytes(b"\xff\xd8test-image")
    assert client.get("/family/manual/image/test-manual-shot.jpg").status_code == 403
    ok = family_client.get("/family/manual/image/test-manual-shot.jpg")
    assert ok.status_code == 200 and ok.content == b"\xff\xd8test-image"


def test_家族マニュアル画像は宅内LANでも配信されない(web, lan_client):
    """**ページ本体と同じ認可にする、という 2026-10-15 の判断の回帰テスト。**

    `_check_tablet_access` を混ぜると、宅内 WiFi に入った第三者（来訪者・介護者）に
    ファイル名の列挙で取得される。ファイル名は public README で既知。
    祖母のタブレットは家族マニュアルを開かないので、通す必要が無い。
    「画像が表示されない」という理由で `or _check_tablet_access(request)` を
    足すと、ここが落ちる。
    """
    (web.manual_dir / "test-manual-shot.jpg").write_bytes(b"\xff\xd8test-image")
    assert lan_client.get("/family/manual/image/test-manual-shot.jpg").status_code == 403


def test_家族マニュアル画像はjpg以外とパストラバーサルを弾く(web, client):
    """`.env` や DB を読み出す踏み台にされないこと。

    HTTP クライアント側で `..` が正規化されてしまい経路テストでは届かないので、
    ハンドラを直接呼んでガードそのものを見る。
    """
    import src.web.app as app_module

    # 拡張子ガードは HTTP 経路でも効く
    assert client.get("/family/manual/image/test.png").status_code == 400

    for bad in ("../../.env", "../data/iot.db", "sub/dir.jpg", "shot.jpg.png", "shot"):
        with pytest.raises(Exception) as exc:
            asyncio.run(app_module.serve_manual_image(_fake_request(EXTERNAL_HOST), bad))
        assert getattr(exc.value, "status_code", None) == 400, f"{bad!r} が 400 で弾かれていない"

    # 名前が正当なら、次のゲートは認可（403）になる = ガードの順序が正しい
    with pytest.raises(Exception) as exc:
        asyncio.run(app_module.serve_manual_image(_fake_request(EXTERNAL_HOST), "ok.jpg"))
    assert getattr(exc.value, "status_code", None) == 403


# ============================================================
# 5. 食事写真の3経路
# ============================================================

def _photo(web, name: str = "901_1760000000.jpg") -> str:
    (web.photos_dir / name).write_bytes(b"\xff\xd8test-meal-photo")
    return name


def test_食事写真は家族セッションで配信される(web, family_client):
    name = _photo(web)
    resp = family_client.get(f"/photos/{name}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"


def test_食事写真はタブレット経路で配信される(web):
    """祖母のタブレットは撮った写真を自分の画面で確認する。"""
    name = _photo(web)
    with _make_client(web, LAN_HOST) as c:
        assert c.get(f"/photos/{name}").status_code == 200
    with _make_client(web) as c:
        assert c.get(f"/photos/{name}", params={"token": TABLET_TOKEN}).status_code == 200


def test_食事写真は正しい署名URLなら配信される(web, client):
    """LINE に送る経路。LINE のサーバが取りに来るので Cookie が無い。"""
    name = _photo(web)
    exp = int(time.time()) + 3600
    sig = web.module._photo_signature(name, exp)
    assert client.get(f"/photos/{name}", params={"exp": exp, "sig": sig}).status_code == 200


@pytest.mark.parametrize("flaw", ["署名なし", "署名が空", "署名改竄", "期限切れ",
                                  "別ファイルの署名", "expが数値でない"])
def test_食事写真は壊れた署名を弾く(web, client, flaw):
    """2026-09-26 時点では認証が一切無く、公開URLから誰でも取得できた。

    ファイル名が `{session_id}_{UNIX秒}.jpg` で列挙可能なので、
    「ファイル名を知らないから安全」は成り立たない。
    HMAC の検証を外す / `==` 比較に変える / 期限チェックを消すと落ちる。
    """
    name = _photo(web)
    now = int(time.time())
    good_exp = now + 3600
    params = {
        "署名なし": {},
        "署名が空": {"exp": good_exp, "sig": ""},
        "署名改竄": {"exp": good_exp, "sig": "f" * 32},
        "期限切れ": {"exp": now - 10, "sig": web.module._photo_signature(name, now - 10)},
        "別ファイルの署名": {"exp": good_exp,
                             "sig": web.module._photo_signature("other_1.jpg", good_exp)},
        "expが数値でない": {"exp": "9999999999x", "sig": "f" * 32},
    }[flaw]
    resp = client.get(f"/photos/{name}", params=params)
    assert resp.status_code == 404, f"{flaw} で写真が配信された: {resp.status_code}"


def test_署名の有効期限を自分で伸ばせない(web, client):
    """exp はクエリで来るので、期限だけ書き換えれば永久URLになる、を防げているか。

    exp も署名の対象に入っていること（`f"{file_name}:{exp}"`）。
    """
    name = _photo(web)
    exp = int(time.time()) + 3600
    sig = web.module._photo_signature(name, exp)
    forever = int(time.time()) + 10 * 365 * 24 * 3600
    assert client.get(f"/photos/{name}", params={"exp": forever, "sig": sig}).status_code == 404


def test_家族が削除した食事写真は誰にも配信されない(web, family_client):
    """家族は「消した」と思っている。deleted_at を立てるだけだと、
    URL を知っている第三者と LINE のトーク履歴から取得され続ける（2026-09-26）。

    署名URLは7日有効なので、削除後も実ファイルが残れば署名は通る。
    DB の deleted_at チェックが消えると落ちる。
    """
    name = _photo(web)
    conn = sqlite3.connect(web.db_path)
    conn.execute(
        "INSERT INTO meal_photos(session_id, person_id, file_name, taken_at, deleted_at)"
        " VALUES(NULL, NULL, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (name,),
    )
    conn.commit()
    conn.close()

    assert family_client.get(f"/photos/{name}").status_code == 404
    exp = int(time.time()) + 3600
    sig = web.module._photo_signature(name, exp)
    with _make_client(web) as c:
        assert c.get(f"/photos/{name}", params={"exp": exp, "sig": sig}).status_code == 404


# ============================================================
# 6. LINE webhook の署名検証
# ============================================================

def _line_body(text: str, sender: str) -> bytes:
    return json.dumps({
        "events": [{
            "type": "message",
            "replyToken": "test-reply-token",
            "source": {"userId": sender},
            "message": {"type": "text", "text": text},
        }]
    }).encode()


def _line_sig(body: bytes, secret: str = LINE_SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


@pytest.fixture
def line_spy(monkeypatch):
    """webhook から先（コマンド処理）に渡ったかだけを記録する。

    `app.py` はハンドラ内で `from ..line_commands import dispatch` しているので、
    モジュール属性を差し替えれば呼び出し時に解決される。
    本物の dispatch は DB を書き、LINE に返信し、炊飯器のロック解除まで行くので
    認可テストで通してはいけない。
    """
    import src.line_commands as line_commands

    seen: list[tuple[str, str]] = []

    async def spy(text, sender_id):
        seen.append((text, sender_id))
        return None

    monkeypatch.setattr(line_commands, "dispatch", spy)
    return seen


@pytest.mark.parametrize("header", [None, "", "not-base64", "AAAAAAAAAAAAAAAAAAAAAA=="])
def test_LINE_webhookは署名が無い不正なリクエストを401で弾く(client, line_spy, header):
    """シークレットが設定されている状態での署名検証。

    webhook は公開エンドポイントで、ここから `登録解除` や炊飯器の
    ロック解除確認まで届く。署名検証が外れると、誰でも LINE からの
    メッセージを偽造して祖母の記録とデバイスを操作できる。
    """
    body = _line_body("リンク", REGISTERED_SENDER)
    headers = {"content-type": "application/json"}
    if header is not None:
        headers["x-line-signature"] = header
    resp = client.post("/line/webhook", content=body, headers=headers)
    assert resp.status_code == 401, f"署名 {header!r} が通った"
    assert line_spy == [], "署名検証前にコマンド処理へ渡っている"


def test_LINE_webhookは正しい署名を通す(client, line_spy):
    body = _line_body("リンク", REGISTERED_SENDER)
    resp = client.post("/line/webhook", content=body,
                       headers={"x-line-signature": _line_sig(body)})
    assert resp.status_code == 200
    assert line_spy == [("リンク", REGISTERED_SENDER)]


def test_LINE_webhookは署名を別の本文に使い回せない(client, line_spy):
    """キャプチャした署名ヘッダを、本文だけ差し替えて再送できないこと。

    署名が本文全体に掛かっていること（ヘッダだけの検証に退化していないこと）を見る。
    """
    captured = _line_sig(_line_body("リンク", REGISTERED_SENDER))
    tampered = _line_body("解除 rice_cooker", REGISTERED_SENDER)
    resp = client.post("/line/webhook", content=tampered,
                       headers={"x-line-signature": captured})
    assert resp.status_code == 401
    assert line_spy == []


# ============================================================
# 7. LINE webhook の許可リスト
# ============================================================

def _post_line(client, text: str, sender: str):
    body = _line_body(text, sender)
    return client.post("/line/webhook", content=body,
                       headers={"x-line-signature": _line_sig(body)})


@pytest.mark.parametrize("text", ["登録一覧", "登録解除", "登録状況", "登録リスト",
                                  "ステータス", "解除", "最後の食事", "ヘルプ"])
def test_未登録の送信者はコマンドを実行できない(client, line_spy, text):
    """2026-10-15 まで判定が `startswith("登録")` だった（commit f569db5 で厳格化）。

    「登録一覧」を送れば未登録の第三者が**家族の氏名一覧**を取得でき、
    「登録解除」で家族を通知先から外せた。LINE の bot は ID を知れば誰でも
    友だち追加できるので、許可リストが唯一の壁。
    """
    resp = _post_line(client, text, STRANGER_SENDER)
    assert resp.status_code == 200, "LINE の再送を招くのでステータスは 200 のまま"
    assert line_spy == [], f"未登録の送信者が {text!r} を実行できた"


@pytest.mark.parametrize("text", ["登録", "登録 テスト母", "登録　テスト母"])
def test_登録コマンドだけは未登録の送信者でも通る(client, line_spy, text):
    """新しい家族が自分を登録する唯一の入口。完全一致「登録」と、
    半角/全角スペース区切りの「登録 <名前>」だけ。
    ここを閉じると家族が増やせなくなるので、厳格化しすぎても困る。
    """
    resp = _post_line(client, text, STRANGER_SENDER)
    assert resp.status_code == 200
    assert line_spy == [(text, STRANGER_SENDER)], f"{text!r} が許可リストで止められた"


def test_登録済みの送信者は普通のコマンドを実行できる(client, line_spy):
    assert _post_line(client, "ステータス", REGISTERED_SENDER).status_code == 200
    assert line_spy == [("ステータス", REGISTERED_SENDER)]


def test_DBに登録された家族は許可リストに含まれる(temp_db):
    """家族登録 (`登録 <名前>`) は DB に行を足す。iot-web を再起動しなくても
    その人が許可されること。DB 参照が落ちると、登録した家族の操作が全部
    「未許可送信者」で無言で捨てられ、本人には何も返らない。

    注意: この関数は `.env` も読むので、戻り値の集合を assert のメッセージに
    出さない（本番の LINE user ID が public リポジトリの CI ログに出る）。
    真偽値にしてから assert する。
    """
    import src.web.app as app_module

    fake_id = "U" + "c" * 32
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT OR REPLACE INTO persons(id, name, role) VALUES(7, 'テスト家族2', 'family')")
    conn.execute(
        "INSERT OR REPLACE INTO family_line_users(line_user_id, person_id, display_name)"
        " VALUES(?, 7, 'テスト家族2')",
        (fake_id,),
    )
    conn.commit()
    conn.close()

    found = fake_id in app_module._load_line_allowed_senders()
    assert found, "DB に登録された家族が許可リストに入っていない"


# ============================================================
# 8. 家族パスワードの門
# ============================================================

def test_空のパスワードでは家族になれない_fail_closed(web, monkeypatch):
    """`.env` から FAMILY_PASSWORD が読めないとき、`_load_family_password()` は
    空文字を返す。そのとき空パスワードでログインできてはいけない。

    今日 `_check_tablet_access` で見つけたのと同じ fail-open のパターン。
    比較を `if not expected or given == expected` のように緩めると落ちる。
    """
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "_load_family_password", lambda: "")
    with _make_client(web) as c:
        for pw in ("", " ", "x"):
            resp = c.post("/family/login", data={"password": pw})
            assert resp.status_code != 303, f"パスワード {pw!r} でログインできた"
        assert c.get("/api/events").status_code == 401


def test_間違ったパスワードはセッションを作らない(web):
    with _make_client(web) as c:
        c.post("/family/login", data={"password": "wrong-password"})
        assert c.get("/api/events").status_code == 401


def test_家族ログインにブルートフォース制限がある(web, monkeypatch):
    """家族UIのパスワードは4桁PIN。公開インターネットに出ているので、
    制限が無いと総当たりで数分で破れる。

    `@limiter.limit("5/15minute")` が外れると落ちる。
    """
    monkeypatch.setattr(web.module.limiter, "enabled", True)
    web.module.limiter.reset()
    try:
        with _make_client(web) as c:
            codes = [
                c.post("/family/login", data={"password": "0000"}).status_code
                for _ in range(7)
            ]
        assert 429 not in codes[:5], f"正当なやり直しまで止めている: {codes}"
        assert 429 in codes[5:], f"総当たりが止まっていない: {codes}"
    finally:
        web.module.limiter.reset()


# ============================================================
# 9. 現状の穴（characterization テスト）
# ============================================================
# 下の2つは**現状の挙動をそのまま固定している**。実装を直す仕事ではないので
# 落ちるテストは残さず、代わりに「ここに穴がある」ことをテストとして記録する。
# 認可を入れたら、このテストを逆向き（弾けること）に書き換えること。

def test_WebSocketは未認証の接続を閉じる(web, client):
    """`/ws/events` は accept() の前に認可する。

    このソケットは家族が祖母に送ったメッセージ本文（family_prompt）を
    そのまま流す。2026-10-15 まで無認証で 101 を返しており、公開 ingress から
    誰でも購読できた（生ソケットのハンドシェイクで実証済み）。
    祖母のタブレットは宅内/ローカルなので _check_tablet_access で通り、
    家族UIは Cookie のセッションで通るため、どちらも壊れない。
    """
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws/events"):
            pass
    assert e.value.code == 1008, "ポリシー違反として閉じること"


def test_家族メッセージの既読化はタブレット認可を要する(web, client):
    """`/api/dismiss-prompt/{id}` だけ、認可チェックが1行も無い。

    同じタブレット用 API の `/api/tablet-record` は
    `_is_local or _check_tablet_access` を通しているのに、ここだけ素通し。
    外部から連続で叩けば、家族が送った priority='critical' の
    「お薬のんでね」が祖母の画面に出る前に消せる。

    2026-10-15 まで 50 エンドポイント中ここだけ素通しで、外部から prompt_id を
    総当たりすれば家族が送った priority='critical' の「お薬のんでね」を
    祖母の画面に出る前に消せた（DB の dismissed が 1 になるところまで実証済み）。
    """
    conn = sqlite3.connect(web.db_path)
    conn.execute(
        "INSERT INTO family_prompts(id, message, sent_by, expires_at, priority)"
        " VALUES(1, 'テスト用メッセージ', 'テスト家族', '2099-01-01 00:00:00', 'critical')"
    )
    conn.commit()
    conn.close()

    assert client.post("/api/dismiss-prompt/1").status_code == 403

    conn = sqlite3.connect(web.db_path)
    dismissed = conn.execute("SELECT dismissed FROM family_prompts WHERE id = 1").fetchone()[0]
    conn.close()
    assert dismissed == 0, "認可を通らない以上、DB は書き換わらないこと"


def test_現状_LINE許可リストが空だと全送信者が通る_既知の穴(client, line_spy, monkeypatch):
    """`if allowed and sender_id not in allowed` — 許可リストが空集合なら
    フィルタ自体が無効になる。

    `.env` の読み取り失敗や DB 例外（`except Exception: pass` で握っている）で
    空集合になると、署名が通る LINE 送信者全員がコマンドを実行できる。
    今日 `_check_tablet_access` で直したのと同じ fail-open のパターンが
    ここに残っている。bot は ID を知れば誰でも友だち追加できる。

    直したら: このテストを「許可リストが空なら全部弾く（登録コマンドを除く）」に
    書き換える。
    """
    import src.web.app as app_module
    monkeypatch.setattr(app_module, "_load_line_allowed_senders", lambda: set())
    assert _post_line(client, "登録一覧", STRANGER_SENDER).status_code == 200
    assert line_spy == [("登録一覧", STRANGER_SENDER)], (
        "穴が閉じたなら、このテストを逆向きに書き換えること"
    )


def test_現状_LINEシークレット未設定なら署名検証が丸ごと飛ぶ_既知の穴(client, line_spy, monkeypatch):
    """`if secret:` — `.env` から LINE_CHANNEL_SECRET が読めないと検証を素通りする。

    コードには「シークレット未設定なら検証スキップ＝自己責任」と書いてあるが、
    `/line/webhook` は公開エンドポイントなので、この状態では**インターネット上の
    誰でも** LINE からのメッセージを偽造して POST できる。
    しかも「登録」は許可リストを免除されるので、偽造リクエストで自分を
    family_line_users に登録 → 以降は許可送信者として扱われる、という連鎖になる。

    `.env` の中身はここでは確認していない（認証情報なので読まない）。
    実運用でシークレットが設定されているかは別途確認が必要。

    直したら: このテストを「シークレット未設定なら全部弾く」に書き換える。
    """
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "_load_line_secret", lambda: "")
    body = _line_body("ステータス", REGISTERED_SENDER)
    resp = client.post("/line/webhook", content=body)  # 署名ヘッダ無し
    assert resp.status_code == 200
    assert line_spy == [("ステータス", REGISTERED_SENDER)], (
        "穴が閉じたなら、このテストを逆向きに書き換えること"
    )
