"""LINE のテキストコマンドが「どの文字列を何として解釈するか」を固定する。

起点の症状は「LINE のコマンドが反応するときとしないときがある」。
応答時間・許可リスト・例外はすでに切り分け済みで、残っているのは
**どの入力が黙って落ちるか**だった。そこを機械的に表にするのがこのファイル。

`dispatch()` は `match_command()` の戻り値 1つで分岐するだけなので、
「反応しない」の正体はほぼ全部 `match_command()` の中にある。実測して分かった形:

  - 部分一致で拾うコマンド（状況 / 様子 / 今日 / 最後 / 解除 / ヘルプ / メニュー）は
    「？」「。」「〜して」「〜お願い」が付いても通る
  - 完全一致で拾うコマンド（登録一覧 / メンバー / 誰が登録 / 解除登録）は
    「？」が1文字付いただけで **unknown → 返信なし**
  同じ人が同じ癖（文末に「？」を付ける）で打っても、コマンドによって
  返ってくる／返ってこないが変わる。これが「反応するときとしないとき」の形と一致する。

  - `"解除" in t` の部分一致が「登録を解除」「通知解除」まで炊飯器の
    ロック解除フローに吸い込む（確認コードが返る）
  - `\\d{4}` は全角数字にもマッチするので「１２３４」は確認コード扱いになり、
    ASCII のコードと比較されて必ず失敗する＝ロックアウト回数が増える

**本番に副作用を出さない。** 送信系（requests.post / reply_line_message /
send_line_message / send_line_with_quick_reply / notify_device_unlocked）と
Matter 操作（unlock_device）は autouse fixture で全部スタブ化する。
DB は conftest の seeded_db（一時ファイル）だけを使う。

テストデータは全部架空。実在の個人名・LINE user ID・トークンは書かない。
実装は直さない方針なので、現状と違う「本来こうあるべき」は
characterization テストとして docstring に書き残す。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import src.line_commands as lc
import src.notifier as notifier
import src.settings as settings
from src.web.app import LINE_URL_TRIGGERS, _is_url_request

# --- 架空のテスト値。実在の ID とは一切関係ない -----------------------------
SENDER = "Utest" + "0" * 28        # 登録済みの「テスト家族」を模擬
SENDER2 = "Utest" + "1" * 28       # もう1人（先勝ち・ロックアウト分離の確認用）
FAKE_BASE_URL = "https://example.test"
FAKE_TOKEN = "test-channel-access-token-not-real"

# autouse fixture でスタブ化する前の本物。送信経路そのものを測る2本だけ戻して使う。
_REAL_REPLY = notifier.reply_line_message
_REAL_QUICK = notifier.send_line_with_quick_reply


def run(coro):
    """async な dispatch をそのまま同期テストから呼ぶ（pytest-asyncio 未導入）。"""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_line_io(monkeypatch):
    """LINE API と Matter に実際には一切触らせない安全弁。

    `handle_feedback` は開発者へ即時転送し、`handle_unlock_confirm` は
    成功時に全家族へ broadcast する。素で呼ぶと家族のスマホが鳴るので、
    送信系は全部ここで潰して「呼ばれた記録」だけ残す。
    requests.post 自体も塞いで、取りこぼしがあれば即落ちるようにする。
    """
    sent: dict[str, list] = {"push": [], "reply": [], "quick": [], "broadcast": [], "post": []}

    def _no_network(*args, **kwargs):
        raise AssertionError("テストが LINE API を実際に叩こうとした")

    monkeypatch.setattr(notifier.requests, "post", _no_network)
    monkeypatch.setattr(notifier, "_load_env", lambda: {})
    monkeypatch.setattr(
        notifier, "send_line_message",
        lambda message, user_id=None: sent["push"].append((user_id, message)) or True,
    )
    monkeypatch.setattr(
        notifier, "reply_line_message",
        lambda reply_token, message: sent["reply"].append((reply_token, message)) or True,
    )
    monkeypatch.setattr(
        notifier, "send_line_with_quick_reply",
        lambda message, quick_items, user_id=None: sent["quick"].append((user_id, message)) or True,
    )
    monkeypatch.setattr(
        notifier, "broadcast_line_message",
        lambda message: sent["broadcast"].append(message) or 1,
    )
    monkeypatch.setattr(
        notifier, "notify_device_unlocked",
        lambda device, manual=False, reason="": sent["broadcast"].append((device, reason)) or True,
    )
    monkeypatch.setattr(notifier, "_admin_user_id", lambda: "Utest" + "9" * 28)
    monkeypatch.setattr(notifier, "_admin_display_name", lambda: "テスト開発者")

    async def _fake_unlock(device_name, node_id, reason=""):
        sent["post"].append(("unlock_device", device_name, reason))
        return True

    monkeypatch.setattr(lc, "unlock_device", _fake_unlock)
    # 本物の data/tunnel_url.txt（公開 URL）をテスト出力に出さない
    monkeypatch.setattr(lc, "_load_public_base_url", lambda: FAKE_BASE_URL)

    # プロセス内メモリの確認コード／ロックアウトはテスト間で持ち越さない
    lc._pending_unlocks.clear()
    lc._lockouts.clear()
    yield sent
    lc._pending_unlocks.clear()
    lc._lockouts.clear()


@pytest.fixture
def db(seeded_db):
    """persons だけ入った一時 DB（id=0 未確定 / 1 テスト祖母 / 3 テスト家族）。"""
    return seeded_db


def _lock_rice(db) -> None:
    """炊飯器をロック状態にする（「ロック解除」が確認コードを返す前提を作る）。"""
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO device_state(device_name, is_locked) VALUES('rice_cooker', 1)"
    )
    conn.commit()
    conn.close()


# ============================================================
# 1. コマンド一覧の全数 — match_command の分岐を取りこぼさない
# ============================================================

# match_command が返しうる全コマンド名。実装に新しい分岐が増えたら
# 下の test_コマンド名の全数が実装と一致していること が落ちる。
KNOWN_COMMANDS = {
    "register_family",
    "unregister_family",
    "list_registered",
    "unlock_request",
    "unlock_confirm",
    "status",
    "last_meal",
    "help",
    "menu",
    "feedback",
    "link",
    "unknown",
}

# dispatch が返信を作らない（None になる）コマンド名。
# 家族から見ると「無反応」になる経路はこの2つだけ。
NO_REPLY_COMMANDS = {
    "unknown",   # どの分岐にも当たらなかった
    "link",      # dispatch は None。webhook 側の _is_url_request が拾い直す
}


def _commands_in_source() -> set[str]:
    """match_command() の本体から `return "..."` の文字列を機械的に拾う。"""
    src = Path(lc.__file__).read_text(encoding="utf-8")
    body = src.split("def match_command(", 1)[1].split("\ndef ", 1)[0]
    return set(re.findall(r'return "([a-z_]+)"', body))


def test_コマンド名の全数が実装と一致していること():
    """分岐が増減したらこのテストで気付けるようにする。

    「どの入力が黙って無視されるか」を数え上げるのが目的なので、
    数え上げ元の一覧が古くなったままだと表そのものが嘘になる。
    """
    assert _commands_in_source() == KNOWN_COMMANDS


# (入力, 期待されるコマンド名) の実測表。
# 「返信あり/黙って無視」はコマンド名から決まる（NO_REPLY_COMMANDS 参照）。
COMMAND_TABLE: list[tuple[str, str]] = [
    # --- 登録（完全一致 or 「登録␣」始まり）---------------------------------
    ("登録", "register_family"),
    ("登録 母", "register_family"),
    ("登録　母", "register_family"),          # 全角空白も入口は通る
    ("登録  母", "register_family"),          # 半角空白2つ
    ("登録 ", "register_family"),
    ("登録母", "unknown"),                    # 空白を入れ忘れると黙って無視
    ("登録します", "unknown"),
    ("登録お願いします", "unknown"),
    ("とうろく 母", "unknown"),
    # --- 登録解除 -----------------------------------------------------------
    ("登録解除", "unregister_family"),
    ("登録解除して", "unregister_family"),     # 前方一致なので丁寧形も通る
    ("解除登録", "unregister_family"),         # 完全一致だけの別名
    ("解除登録して", "unlock_request"),        # ← 1文字足すと炊飯器側へ falls through
    ("登録を解除", "unlock_request"),          # ← 「解除」部分一致の衝突
    ("登録 解除", "register_family"),          # ← 「解除」という名前の家族登録になる
    # --- 登録一覧（完全一致のみ）-------------------------------------------
    ("登録一覧", "list_registered"),
    ("登録一覧 ", "list_registered"),          # strip されるので前後の空白は可
    ("登録一覧\n", "list_registered"),
    ("登録一覧　", "list_registered"),
    ("登録一覧?", "unknown"),                  # ← 「？」1文字で無反応
    ("登録一覧？", "unknown"),
    ("登録一覧。", "unknown"),
    ("登録一覧を見たい", "unknown"),
    ("メンバー", "list_registered"),
    ("メンバー一覧", "unknown"),               # ← ありがちな言い方が通らない
    ("メンバー?", "unknown"),
    ("誰が登録", "list_registered"),
    ("誰が登録してる?", "unknown"),
    ("誰が登録してますか", "unknown"),
    ("登録してる人", "unknown"),
    # --- ロック解除（部分一致）---------------------------------------------
    ("ロック解除", "unlock_request"),
    ("ロック解除して", "unlock_request"),
    ("ロック解除お願いします", "unlock_request"),
    ("ロック解除？", "unlock_request"),
    ("ロック 解除", "unlock_request"),
    ("ろっく解除", "unlock_request"),
    ("炊飯器のロックを解除して", "unlock_request"),
    ("解除", "unlock_request"),
    ("解除して", "unlock_request"),
    ("通知解除", "unlock_request"),            # ← 通知を止めたい人に解除コードが返る
    ("アンロック", "unlock_request"),
    ("あんろっく", "unknown"),                 # ひらがなは通らない
    ("ロックかいじょ", "unknown"),
    ("ろっくかいじょ", "unknown"),
    ("ロックを外して", "unknown"),
    ("炊飯器あけて", "unknown"),
    ("ごはん炊きたい", "unknown"),
    # --- 確認コード（\d{4} 完全一致）---------------------------------------
    ("1234", "unlock_confirm"),
    (" 1234 ", "unlock_confirm"),
    ("1234\n", "unlock_confirm"),
    ("１２３４", "unlock_confirm"),            # ← 全角数字もここに入る（必ず不一致）
    ("0000", "unlock_confirm"),
    ("2026", "unlock_confirm"),               # 西暦を打っただけでも入る
    ("123", "unknown"),
    ("12345", "unknown"),
    ("コード1234", "unknown"),
    # --- 状況（部分一致）---------------------------------------------------
    ("状況", "status"),
    ("状況?", "status"),
    ("状況？", "status"),
    ("状況。", "status"),
    ("状況を教えて", "status"),
    ("　状況　", "status"),
    ("状況\t", "status"),
    ("様子", "status"),
    ("様子は?", "status"),
    ("今日", "status"),
    ("今日は?", "status"),
    ("今日どう?", "status"),
    ("今日の様子", "status"),
    ("今日の食事", "status"),                  # ← 「食事」なのに status が勝つ
    ("じょうきょう", "unknown"),
    ("ジョウキョウ", "unknown"),
    ("ステータス", "unknown"),                 # ← よくある言い換えが無反応
    ("状態", "unknown"),
    ("状態は?", "unknown"),
    ("いまどう?", "unknown"),
    ("今どう？", "unknown"),
    ("元気?", "unknown"),
    ("大丈夫?", "unknown"),
    ("status", "unknown"),
    ("ｓｔａｔｕｓ", "unknown"),
    # --- 最後の食事 ---------------------------------------------------------
    ("最後の食事", "last_meal"),
    ("最後", "last_meal"),
    ("最後の食事は?", "last_meal"),
    ("最後の食事\n", "last_meal"),
    ("さっき", "last_meal"),
    ("さっきの食事", "last_meal"),
    ("直近の食事", "unknown"),
    ("前回の食事", "unknown"),
    ("ごはん食べた?", "unknown"),
    ("ご飯いつ", "unknown"),
    ("食事", "unknown"),
    ("朝食食べた?", "unknown"),
    # --- ヘルプ -------------------------------------------------------------
    ("ヘルプ", "help"),
    ("ヘルプ!", "help"),
    ("ヘルプ\n", "help"),
    ("ヘルプお願い", "help"),
    ("help", "help"),
    ("Help", "help"),
    ("HELP", "help"),
    ("コマンド", "help"),
    ("コマンド一覧", "help"),
    ("使い方", "help"),
    ("へるぷ", "unknown"),                     # ひらがなは通らない
    ("使いかた", "unknown"),
    ("つかいかた", "unknown"),
    ("ｈｅｌｐ", "unknown"),                    # ← 全角英数は通らない
    ("ＨＥＬＰ", "unknown"),
    ("何ができる?", "unknown"),
    ("?", "unknown"),
    ("？", "unknown"),
    # --- メニュー -----------------------------------------------------------
    ("メニュー", "menu"),
    ("メニュー\n", "menu"),
    ("メニュー出して", "menu"),
    ("menu", "menu"),
    ("Menu", "menu"),
    ("MENU", "menu"),
    ("めにゅー", "unknown"),
    ("ｍｅｎｕ", "unknown"),
    ("ボタン", "unknown"),
    # --- 意見・質問（前方一致）---------------------------------------------
    ("意見", "feedback"),
    ("意見 写真が送れない", "feedback"),
    ("意見　写真が送れない", "feedback"),
    ("意見です", "feedback"),
    ("意見があります", "feedback"),
    ("質問", "feedback"),
    ("質問 通知を減らせますか", "feedback"),
    ("要望 機能追加", "feedback"),
    ("バグ 画面が白い", "feedback"),
    ("ご意見", "unknown"),                     # 前方一致なので1文字前に付くと落ちる
    ("相談 ちょっと聞きたい", "unknown"),
    # --- リンク（dispatch は None。webhook が拾い直す）---------------------
    ("リンク", "link"),
    ("リンク ", "link"),
    ("リンク送って", "link"),
    ("URL", "link"),
    ("url", "link"),
    ("アクセス", "link"),
    ("アクセスできない", "link"),
    ("つながらない", "link"),
    ("繋がらない", "link"),
    ("見れない", "link"),
    ("みれない", "link"),
    ("開けない", "link"),
    ("接続", "link"),
    ("つながらん", "unknown"),                 # webhook 側のみが拾う（表記ゆれ）
    ("見られない", "unknown"),
    ("みられない", "unknown"),
    ("あけない", "unknown"),
    ("link", "unknown"),                      # ← 英語の link はどちらも拾わない
    ("りんく", "unknown"),
    ("ＵＲＬ", "unknown"),                      # ← 全角 URL はどちらも拾わない
    ("画面が開かない", "unknown"),
    ("ひらけない", "unknown"),
    # --- 境界 ---------------------------------------------------------------
    ("", "unknown"),
    (" ", "unknown"),
    ("　", "unknown"),
    ("\n", "unknown"),
    ("  \n ", "unknown"),
    ("あ" * 1000, "unknown"),
    ("😀", "unknown"),
    ("👍🙏", "unknown"),
    ("。", "unknown"),
    ("https://example.test/x", "unknown"),
    # --- 生活の中で普通に送られる文（全部無反応）---------------------------
    ("ありがとう", "unknown"),
    ("おはよう", "unknown"),
    ("わかりました", "unknown"),
    ("はい", "unknown"),
    ("OK", "unknown"),
    ("了解", "unknown"),
    ("すみません", "unknown"),
    ("ごはん", "unknown"),
    ("お風呂", "unknown"),
    ("トイレ", "unknown"),
    ("薬", "unknown"),
    ("写真", "unknown"),
]


@pytest.mark.parametrize("text,expected", COMMAND_TABLE, ids=lambda v: repr(v)[:28])
def test_コマンド解釈の実測表(text, expected):
    """どの文字列がどのコマンドになるかを1件ずつ固定する。

    これが今回の成果物そのもの。expected は実装の**現状**で、
    「本来こうあるべき」ではない。意図的に直したときは落ちるので、
    そのときは表を更新する（= 変更が可視化される）。
    """
    assert lc.match_command(text) == expected


def test_表が全コマンドを少なくとも1件ずつ覆っていること():
    """表にコマンドの取りこぼしがあると「網羅した」と言えなくなる。"""
    covered = {expected for _, expected in COMMAND_TABLE}
    assert covered == KNOWN_COMMANDS


# ============================================================
# 2. 返信が無い経路 — 「反応しない」の正体
# ============================================================

# 家族が自然に打ちそうなのに、dispatch も webhook の URL 判定も拾わない文字列。
# ここに並んでいるものは全部「送っても何も返ってこない」。
SILENTLY_IGNORED = [
    "ステータス",
    "状態",
    "状態は?",
    "いまどう?",
    "今どう？",
    "元気?",
    "大丈夫?",
    "登録一覧?",
    "登録一覧？",
    "メンバー一覧",
    "誰が登録してる?",
    "直近の食事",
    "前回の食事",
    "ごはん食べた?",
    "食事",
    "へるぷ",
    "使いかた",
    "めにゅー",
    "ｈｅｌｐ",
    "ｍｅｎｕ",
    "ＵＲＬ",
    "link",
    "りんく",
    "登録母",
    "登録お願いします",
    "ロックを外して",
    "炊飯器あけて",
    "ご意見",
    "相談 ちょっと聞きたい",
    "何ができる?",
    "",
    " ",
    "　",
    "\n",
    "あ" * 1000,
    "😀",
    "https://example.test/x",
    "ありがとう",
    "おはよう",
    "はい",
]


@pytest.mark.parametrize("text", SILENTLY_IGNORED, ids=lambda v: repr(v)[:24])
def test_黙って無視される入力(text, db):
    """「反応しない」と言われる入力を列挙して固定する。

    dispatch が None を返し、webhook 側の `_is_url_request` も False の
    組み合わせだけが本当の無反応。送った家族にはエラーも何も出ない。
    """
    assert run(lc.dispatch(text, SENDER)) is None
    assert _is_url_request(text) is False


def test_dispatch_が_None_を返すのは_unknown_と_link_だけ(db):
    """返信が無い経路を網羅的に確定させる。

    各コマンドの代表入力を1件ずつ通して、返信が None になるのが
    unknown と link のときだけであることを確認する。
    """
    _lock_rice(db)
    representative = {
        "register_family": "登録 テスト母",
        "unregister_family": "登録解除",
        "list_registered": "登録一覧",
        "unlock_request": "ロック解除",
        "unlock_confirm": "1234",
        "status": "状況",
        "last_meal": "最後の食事",
        "help": "ヘルプ",
        "menu": "メニュー",
        "feedback": "意見 テストです",
        "link": "リンク",
        "unknown": "ありがとう",
    }
    assert set(representative) == KNOWN_COMMANDS
    none_cmds = set()
    for cmd, text in representative.items():
        assert lc.match_command(text) == cmd, text
        if run(lc.dispatch(text, SENDER)) is None:
            none_cmds.add(cmd)
    assert none_cmds == NO_REPLY_COMMANDS


def test_リンクは_dispatch_では処理されず_webhook_の取りこぼしに依存する(db):
    """「リンク」は dispatch の外で処理される、という設計を固定する。

    `dispatch()` の docstring どおりの意図的な作りだが、返信を組み立てるのは
    webhook だけなので、`/api/...` など dispatch を直接呼ぶ経路が将来増えると
    「リンク」だけ無反応になる。`match_command` の link キーワードが
    webhook の LINE_URL_TRIGGERS の部分集合であることもここで固定する。
    """
    assert run(lc.dispatch("リンク", SENDER)) is None
    assert _is_url_request("リンク") is True

    body = Path(lc.__file__).read_text(encoding="utf-8").split("def match_command(", 1)[1]
    link_block = body.split('return "link"', 1)[0].rsplit("if any(", 1)[1]
    link_kws = re.findall(r'"([^"]+)"', link_block)
    assert link_kws, "link キーワードの抽出に失敗"
    triggers = {t.lower() for t in LINE_URL_TRIGGERS}
    assert {k.lower() for k in link_kws} <= triggers


# ============================================================
# 3. 部分一致の罠 — 「解除」が炊飯器のロック解除に吸い込む
# ============================================================

# 「解除」を含むが、炊飯器のロック解除を頼んでいるわけではない文。
NOT_RICE_UNLOCK = [
    "登録を解除",
    "通知解除",
    "通知を解除してほしい",
    "解除登録して",
    "アラート解除",
    "解除したい",
]


@pytest.mark.parametrize("text", NOT_RICE_UNLOCK, ids=lambda v: repr(v)[:24])
def test_解除の部分一致が炊飯器のロック解除に吸い込まれる(text, db):
    """`any(kw in t for kw in (..., "解除", ...))` の衝突を固定する。

    本来は「登録解除」「通知停止」の話なのに、炊飯器の4桁確認コードが返る。
    家族は何のコードを渡されたのか分からず、別の意図でこのコードを返信すると
    実際に炊飯器のロックが解除される。
    直したら: 「解除」単独の部分一致を外し、「ロック解除」「アンロック」と
    「登録解除」を別々の完全一致／前方一致で判定するよう書き換える。
    """
    _lock_rice(db)
    assert lc.match_command(text) == "unlock_request"
    reply = run(lc.dispatch(text, SENDER))
    assert "確認コード" in reply
    assert lc._pending_unlocks[SENDER]["device"] == "rice_cooker"


def test_登録解除は確認なしで即時に通知を止める(db):
    """「登録解除して」が前方一致で即 DELETE されることを固定する。

    ロック解除には4桁の確認コードがあるのに、通知が止まる登録解除には
    確認が無い。誤爆すると、その家族には以後一切 LINE 通知が届かなくなる
    （本人は解除したつもりが無いので気付けない）。
    """
    run(lc.dispatch("登録 テスト母", SENDER))
    assert "テスト母" in lc.handle_list_registered()

    reply = run(lc.dispatch("登録解除して", SENDER))
    assert "解除しました" in reply
    assert "テスト母" not in lc.handle_list_registered()


def test_登録スペース解除は解除という名前の家族を作ってしまう(db):
    """「登録 解除」が register_family に入る衝突を固定する。

    webhook 側の `is_register_command`（未登録者を通す条件）も
    `startswith("登録 ")` なので、未登録の送信者でもここまで到達できる。
    persons に「解除」という行が増え、以後の通知名が「解除さん」になる。
    """
    assert lc.match_command("登録 解除") == "register_family"
    reply = run(lc.dispatch("登録 解除", SENDER))
    assert "「解除」として登録しました" in reply
    assert "解除" in lc.handle_list_registered()


def test_今日の食事は最後の食事ではなく状況を返す(db):
    """「今日」の部分一致が「最後の食事」より先に当たることを固定する。

    `"今日" in t` が `"最後" in t` より前にあるため、「今日の食事」と
    「今日の最後の食事」はどちらも status になる。実害は小さいが、
    家族から見ると「聞いたことと違う答えが返る」挙動。
    """
    assert lc.match_command("今日の食事") == "status"
    assert lc.match_command("今日の最後の食事") == "status"
    assert lc.match_command("最後の食事") == "last_meal"
    reply = run(lc.dispatch("今日の食事", SENDER))
    assert reply.startswith("📊 今日の祖母の状況")


# ============================================================
# 4. 確認コード — 全角数字が必ず失敗してロックアウトを進める
# ============================================================

def test_全角数字の確認コードは必ず失敗してロックアウトに近づく(db):
    """`re.fullmatch(r"\\d{4}", "１２３４")` が真になることの帰結を固定する。

    `\\d` は Unicode の数字にマッチするので全角数字も unlock_confirm に入る。
    一方 `text.strip() != pending["code"]` は ASCII の生文字列比較なので
    絶対に一致しない。日本語 IME のまま打った家族は、正しいコードを
    写しているのに「コードが違います」と言われ、3回でロックアウトされる。
    直したら: 比較の前に `unicodedata.normalize("NFKC", text)` を通す
    （照合側だけ直すと全角で発行されたコードと噛み合わないので、
    発行コードは ASCII 固定のまま照合を正規化する）。
    """
    _lock_rice(db)
    issued = run(lc.dispatch("ロック解除", SENDER))
    code = re.search(r"確認コード: (\d{4})", issued).group(1)

    fullwidth = code.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    assert lc.match_command(fullwidth) == "unlock_confirm"

    reply = run(lc.dispatch(fullwidth, SENDER))
    assert "コードが違います" in reply
    assert lc._lockouts[SENDER]["count"] == 1

    # 半角で打ち直せば通る（= コード自体は合っていた）
    ok = run(lc.dispatch(code, SENDER))
    assert ok == "✅ 炊飯器のロックを解除しました。"
    assert SENDER not in lc._lockouts


def test_3回失敗するとロックアウトされるが止まるのはロック解除だけ(db):
    """ロックアウトの文面と実際の効果のずれを固定する。

    文面は「{mins}分間コマンドをロックします」だが、`_is_locked_out()` を
    見ているのは `handle_unlock_request` だけ。状況・ヘルプ等は普通に通る。
    家族には「全部効かなくなった」と読めるので、問い合わせの原因になる。
    直したら: 文面を「ロック解除を{mins}分間受け付けません」に直すか、
    dispatch 入口でロックアウトを見るようにする（後者は仕様変更）。
    """
    _lock_rice(db)
    run(lc.dispatch("ロック解除", SENDER))
    for _ in range(lc.LOCKOUT_THRESHOLD - 1):
        assert "コードが違います" in run(lc.dispatch("9999", SENDER))
    last = run(lc.dispatch("9999", SENDER))
    assert "分間コマンドをロックします" in last

    # 実際に止まるのは unlock_request だけ
    assert "ロック中です" in run(lc.dispatch("ロック解除", SENDER))
    assert run(lc.dispatch("状況", SENDER)).startswith("📊")
    assert run(lc.dispatch("ヘルプ", SENDER)).startswith("📖")
    # ロックアウトは送信者ごと（他の家族は巻き込まれない）
    assert "確認コード" in run(lc.dispatch("ロック解除", SENDER2))


def test_4桁の数字は発行前でも確認コードとして扱われる(db):
    """西暦や部屋番号を打っただけで確認コードの経路に入ることを固定する。

    返信自体は出るので無反応ではないが、文脈のない「確認コードは
    発行されていません」が返るため、家族には脈絡のない返事に見える。
    """
    assert "確認コードは発行されていません" in run(lc.dispatch("2026", SENDER))
    assert "確認コードは発行されていません" in run(lc.dispatch("0000", SENDER))


def test_確認コードの失敗回数は時間で戻らない(db):
    """`_lockouts[*]["count"]` に減衰が無いことを固定する。

    プロセスが生きている限り、昨日の失敗2回＋今日の1回で即ロックアウトされる。
    `until` の期限切れ時は dict を丸ごと削除するので count も消えるが、
    しきい値に届かなかった count は永久に残る。
    直したら: 失敗の最終時刻を持って一定時間で count をリセットする。
    """
    _lock_rice(db)
    run(lc.dispatch("ロック解除", SENDER))
    run(lc.dispatch("9999", SENDER))
    assert lc._lockouts[SENDER]["count"] == 1
    assert lc._lockouts[SENDER]["until"] == 0  # 期限が無いので時間では消えない


# ============================================================
# 5. ヘルプの文面と、実際に動くコマンドの突き合わせ
# ============================================================

# handle_help() の「」内にあるコマンド名 → 実際に試す文字列
HELP_SAMPLES = {
    "メニュー": "メニュー",
    "登録 <名前>": "登録 テスト母",
    "登録解除": "登録解除",
    "登録一覧": "登録一覧",
    "状況": "状況",
    "最後の食事": "最後の食事",
    "リンク": "リンク",
    "ロック解除": "ロック解除",
    "意見 ＜内容＞": "意見 テストです",
    "ヘルプ": "ヘルプ",
}


def _help_entries() -> list[str]:
    """ヘルプ文面から「」で囲まれたコマンド名を抜き出す。"""
    return re.findall(r"「([^」]+)」", lc.handle_help())


def test_ヘルプに載っているコマンド名を取りこぼしていないこと():
    """ヘルプの文面が変わったら、この突き合わせ表も直させる。"""
    assert set(_help_entries()) == set(HELP_SAMPLES)


@pytest.mark.parametrize("entry", list(HELP_SAMPLES), ids=lambda v: v)
def test_ヘルプ記載のコマンドは_リンク以外すべて_dispatch_で動く(entry, db):
    """載っているのに動かないものを洗い出す。

    実測の結果、ヘルプ記載の10個のうち `dispatch()` が返信を作らないのは
    「リンク」だけ（webhook の `_is_url_request` が別経路で拾うので、
    LINE から送る限りは返信が届く）。
    """
    _lock_rice(db)
    reply = run(lc.dispatch(HELP_SAMPLES[entry], SENDER))
    if entry == "リンク":
        assert reply is None
        assert _is_url_request(HELP_SAMPLES[entry]) is True
    else:
        assert reply is not None and reply != ""


# 動くのにヘルプに載っていない入力（家族は存在を知らない）。
UNDOCUMENTED_BUT_WORKS = [
    ("様子", "status"),
    ("今日", "status"),
    ("さっき", "last_meal"),
    ("最後", "last_meal"),
    ("解除", "unlock_request"),
    ("アンロック", "unlock_request"),
    ("メンバー", "list_registered"),
    ("誰が登録", "list_registered"),
    ("解除登録", "unregister_family"),
    ("help", "help"),
    ("コマンド", "help"),
    ("使い方", "help"),
    ("menu", "menu"),
    ("質問 通知を減らせますか", "feedback"),
    ("要望 機能追加", "feedback"),
    ("バグ 画面が白い", "feedback"),
    ("URL", "link"),
    ("アクセス", "link"),
    ("つながらない", "link"),
    ("見れない", "link"),
    ("開けない", "link"),
    ("接続", "link"),
]


@pytest.mark.parametrize("text,cmd", UNDOCUMENTED_BUT_WORKS, ids=lambda v: repr(v)[:24])
def test_ヘルプ未掲載でも動くコマンド(text, cmd, db):
    """ヘルプに無いのに反応する入力を固定する。

    「解除」や「今日」のように当たり判定が広いものが載っていないので、
    家族は「なぜか反応した／しなかった」の理由を知ることができない。
    ヘルプは4桁の確認コードを返信する手順にも触れていない。
    """
    assert lc.match_command(text) == cmd
    assert text not in _help_entries()


def test_ヘルプは確認コードの返信手順に触れていない():
    """ロック解除の2段階目（4桁返信）がヘルプに無いことを固定する。

    発行時のメッセージには書いてあるが、ヘルプだけ読んだ家族は
    「ロック解除」と送って返ってきた4桁を何に使うのか分からない。
    """
    help_text = lc.handle_help()
    assert "確認コード付き" in help_text  # 括弧書きの言及だけはある
    assert "4桁" not in help_text and "返信" not in help_text


# ============================================================
# 6. メニュー — dispatch だけ戻り型が違い、push 経路に落ちる
# ============================================================

def test_メニューだけ_dispatch_が文字列ではなく辞書を返す(db):
    """`dispatch() -> str | None` の型注釈と実際の戻り値のずれを固定する。

    webhook は `isinstance(reply, dict)` で拾えているが、
    `reply` を文字列として扱う呼び出し元を増やすと壊れる。
    """
    reply = run(lc.dispatch("メニュー", SENDER))
    assert isinstance(reply, dict)
    assert reply["_type"] == "menu"
    assert [i["label"] for i in reply["items"] if i.get("data")] == ["💬 意見・質問"]
    assert reply["items"][0]["uri"].startswith(FAKE_BASE_URL)


def test_メニューは通知マスタースイッチがOFFだと無反応になる(db, monkeypatch):
    """「反応するときとしないときがある」に最も形が近い経路。

    webhook はメニューだけ reply API ではなく push
    (`send_line_with_quick_reply`) で返す。push 側はマスタースイッチを
    見てから送るので、スイッチ OFF の間は「メニュー」だけ無反応になり、
    「状況」「ヘルプ」は reply API なので普通に返る。
    settings の説明文は「OFFで全Push通知を停止。Webhook返信は継続」と
    書いてあるので、意図に反している。
    直したら: メニューも reply_token で返す（Quick Reply は reply API でも送れる）。
    """
    recorded: list = []
    monkeypatch.setattr(notifier, "reply_line_message", _REAL_REPLY)
    monkeypatch.setattr(notifier, "send_line_with_quick_reply", _REAL_QUICK)
    monkeypatch.setattr(
        notifier, "_load_env",
        lambda: {"LINE_CHANNEL_ACCESS_TOKEN": FAKE_TOKEN, "LINE_USER_ID": SENDER},
    )
    monkeypatch.setattr(
        notifier.requests, "post",
        lambda *a, **k: recorded.append(k.get("json")) or _Resp(200),
    )
    settings.set_setting("notify_master_enabled", "0")

    # push 経路（メニュー）は1件も送られない
    assert notifier.send_line_with_quick_reply("📋 メニュー", [{"label": "x", "data": "y"}],
                                               user_id=SENDER) is False
    assert recorded == []

    # reply 経路（状況・ヘルプ）はマスタースイッチを見ないので送られる
    assert notifier.reply_line_message("test-reply-token", lc.handle_help()) is True
    assert len(recorded) == 1


def test_返信はネットワーク断で消えても再送されない(monkeypatch):
    """reply API だけ outbox に退避しないことを固定する。

    `send_line_with_quick_reply` は ConnectionError を outbox に積むが、
    `reply_line_message` は `except Exception: return False` で捨てる。
    reply token は有効期限が短く使い切りなので再送もできない。
    一瞬の回線断＝その1回だけ無反応、という「たまに反応しない」を作る。
    直したら: 返信に失敗したら push にフォールバックする
    （reply token と違い user_id は webhook から分かっている）。
    """
    monkeypatch.setattr(notifier, "reply_line_message", _REAL_REPLY)
    monkeypatch.setattr(
        notifier, "_load_env",
        lambda: {"LINE_CHANNEL_ACCESS_TOKEN": FAKE_TOKEN},
    )

    def _boom(*a, **k):
        raise notifier.requests.exceptions.ConnectionError("テストの回線断")

    monkeypatch.setattr(notifier.requests, "post", _boom)
    outbox = Path(notifier.__file__).resolve().parent.parent / "data" / "line_outbox.jsonl"
    before = outbox.stat().st_mtime if outbox.exists() else None

    assert notifier.reply_line_message("test-reply-token", "状況") is False

    after = outbox.stat().st_mtime if outbox.exists() else None
    assert before == after, "reply は outbox に積まれない（積まれたら実装が変わった）"


class _Resp:
    """requests.Response の最小スタブ。"""

    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = ""


# ============================================================
# 7. 意見・質問 — 本文の切り出しと転送
# ============================================================

def test_意見は本文を切り出して開発者に転送する(db, no_line_io):
    """「意見 <本文>」の本文抽出と転送を固定する。"""
    reply = run(lc.dispatch("意見 写真がうまく送れません", SENDER))
    assert "ありがとうございます" in reply
    assert len(no_line_io["push"]) == 1
    assert "写真がうまく送れません" in no_line_io["push"][0][1]


def test_意見は全角空白区切りでも本文を拾える(db, no_line_io):
    """`str.split(maxsplit=1)` が全角空白も区切りに使うことを固定する。

    日本語 IME のままだと全角空白になりがちなので、ここは通る。
    一方で入口の `startswith("登録 ")` は全角を別扱いしており、
    空白の扱いが機能ごとに揃っていない。
    """
    reply = run(lc.dispatch("質問　通知を減らせますか", SENDER))
    assert "ありがとうございます" in reply
    assert "通知を減らせますか" in no_line_io["push"][0][1]


def test_意見だけ送ると本文なしの案内が返る(db, no_line_io):
    """本文なしは案内に落ちる（無反応ではない）ことを固定する。"""
    for text in ("意見", "質問", "意見です", "意見があります"):
        reply = run(lc.dispatch(text, SENDER))
        assert "意見・質問の送り方" in reply
    assert no_line_io["push"] == []


def test_意見は送信者名が未登録だと誰かとして転送される(db, no_line_io):
    """未登録の送信者でも転送が落ちないことを固定する。

    許可リストを通らない限りここには来ないが、
    許可リストが空のときは誰でも到達する（既知の穴）。
    """
    run(lc.dispatch("意見 テスト", "Utest" + "f" * 28))
    assert no_line_io["push"][0][1].startswith("💬 誰かさんから")


# ============================================================
# 8. 登録コマンドの文字列解釈
# ============================================================

def test_登録は空白の有無で結果が変わる(db):
    """「登録母」だけ無反応になることを固定する。

    `startswith("登録 ")` / `startswith("登録　")` / `== "登録"` の3条件なので、
    空白を入れ忘れた「登録母」はどの分岐にも入らない。
    未登録の送信者にとっては、唯一の入口を1文字で外した状態になり、
    許可リストでも落ちるので二重に無反応。
    """
    assert "📝 家族登録" in run(lc.dispatch("登録", SENDER))
    assert "として登録しました" in run(lc.dispatch("登録 テスト母", SENDER))
    assert run(lc.dispatch("登録母", SENDER)) is None


def test_登録は名前の長さと予約名を弾く(db):
    """異常値が無反応ではなくメッセージになることを固定する。"""
    assert "20文字以内" in run(lc.dispatch("登録 " + "あ" * 21, SENDER))
    assert "予約名" in run(lc.dispatch("登録 未確定", SENDER))


def test_登録は既存の人物名と一致すると同じ行に紐づく(db):
    """「登録 テスト祖母」で祖母の person 行に相乗りできることを固定する。

    名前が一致するだけで既存 persons にリンクするので、
    許可リストが空の時間帯に第三者が祖母や家族の名前で登録すると、
    以後その人の操作が家族名で通知される。
    直したら: 既存 persons へのリンクは家族 UI 側の承認を要するようにする。
    """
    reply = run(lc.dispatch("登録 テスト祖母", SENDER2))
    assert "として登録しました" in reply
    assert "新しい人物として作成しました" not in reply


def test_登録の返信にはヘルプ全文が付く(db):
    """登録直後だけヘルプが出る（以後は自分で呼ぶ必要がある）ことを固定する。"""
    reply = run(lc.dispatch("登録 テスト母", SENDER))
    assert "📖 使えるコマンド" in reply


def test_未登録の登録解除は案内を返す(db):
    """登録していない人の「登録解除」が無反応にならないことを固定する。"""
    assert "まだ登録されていません" in run(lc.dispatch("登録解除", SENDER2))


def test_登録一覧は空でも案内を返す(db):
    """誰も登録していない状態の文面を固定する。"""
    assert "登録済み家族はいません" in run(lc.dispatch("登録一覧", SENDER))


def test_登録一覧はLINEユーザーIDを一部伏せて出す(db):
    """一覧に生の user ID が出ないことを固定する。

    public リポジトリのシステムなので、ここが生のままだと
    スクリーンショット経由で ID が漏れる。
    """
    run(lc.dispatch("登録 テスト母", SENDER))
    listing = run(lc.dispatch("登録一覧", SENDER))
    assert SENDER not in listing
    assert SENDER[:6] + "..." + SENDER[-3:] in listing


# ============================================================
# 9. 状況・最後の食事 — データが無いときの文面
# ============================================================

def test_状況はデータが無くても全項目を返す(db):
    """記録ゼロでも無反応にならないことを固定する。"""
    reply = run(lc.dispatch("状況", SENDER))
    for key in ("🍴 食事回数", "💊 お薬", "🛁 お風呂", "🚽 トイレ", "⭐ スタンプ達成", "🍚 炊飯器"):
        assert key in reply
    assert "（記録なし）" in reply


def test_最後の食事は記録が無いとその旨を返す(db):
    """無反応と区別できるメッセージが返ることを固定する。"""
    assert run(lc.dispatch("最後の食事", SENDER)) == "今日はまだ食事の記録がありません。"


def test_ロック解除はロックされていなければコードを出さない(db):
    """ロックされていない状態の文面を固定する（コードの無駄撃ちを防ぐ）。"""
    reply = run(lc.dispatch("ロック解除", SENDER))
    assert "既にロックされていません" in reply
    assert lc._pending_unlocks == {}


def test_1000文字の中にキーワードが混ざれば反応する(db):
    """長文でも部分一致なら通ることを固定する。

    逆に言うと、雑談の中に「解除」や「今日」が入っただけで
    コマンドとして解釈される（上の衝突テストと同じ原因）。
    """
    long_text = "あ" * 500 + "状況" + "い" * 500
    assert lc.match_command(long_text) == "status"
    assert run(lc.dispatch(long_text, SENDER)).startswith("📊")


def test_登録解除すると許可リストから外れて以後すべて無反応になる(db):
    """「登録解除」の本当の影響範囲を固定する。

    許可リストは `.env` と DB の `family_line_users` の和集合なので、
    `.env` に直接書かれていない家族が「登録解除」を送ると、以後
    **すべてのコマンドが webhook の許可リストで落ちる**（返信も、
    「未登録です」の案内も出ない）。本人は通知を止めたつもりでも、
    状況確認やロック解除まで一切効かなくなる。復旧できるのは
    「登録 <名前>」だけが未登録者でも通るからで、それを知らないと
    「LINE が全部反応しなくなった」状態になる。
    ヘルプに「登録解除」が載っていて確認ステップも無いので、誤爆しやすい。
    直したら: 登録解除に確認ステップを付け、返信に
    「もう一度『登録 <名前>』で戻せます」を必ず添える。

    許可リストの集合そのものは assert に出さない（実在 ID を出力しない）。
    """
    from src.web import app as app_module

    run(lc.dispatch("登録 テスト母", SENDER))
    was_allowed = SENDER in app_module._load_line_allowed_senders()
    assert was_allowed is True

    run(lc.dispatch("登録解除", SENDER))
    still_allowed = SENDER in app_module._load_line_allowed_senders()
    assert still_allowed is False
