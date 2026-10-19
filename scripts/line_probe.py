#!/usr/bin/env python3
"""LINE コマンドを安全に試す。**家族には1通も送らない。**

    venv/bin/python scripts/line_probe.py 状況
    venv/bin/python scripts/line_probe.py --all          # 主要コマンドを一括
    venv/bin/python scripts/line_probe.py --postback rice_action:31:cook

本番 webhook を直接叩いてはいけない。2026-10-15 に reply 失敗時の push
フォールバックを入れたため、ダミーの replyToken で叩くと reply が失敗し、
**家族の LINE に本物の push が飛ぶ**。このスクリプトは送信系を全部
スタブに差し替えてから dispatch を呼ぶので、その事故が起きない。

DB は既定で読み取り専用のコピーを使う（本番を書き換えない）。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAIN_COMMANDS = [
    "状況", "ステータス", "状態",
    "最後の食事", "直近の食事", "ごはん食べた？",
    "ヘルプ", "へるぷ", "ＨＥＬＰ",
    "メニュー", "めにゅー",
    "登録一覧", "登録一覧？", "メンバー一覧",
    "リンク", "りんく", "画面が開けない",
    "ロック解除", "ろっくかいじょ",
    "通知を解除して",          # 炊飯器に吸い込まれないこと
    "ありがとう",              # 未知コマンドの案内が返ること
]

_sent: list[tuple[str, str]] = []


def _install_stubs() -> None:
    """LINE へ出ていく経路を全部塞ぐ。import より先に呼ぶこと。"""
    import src.notifier as notifier

    def rec(kind):
        def f(*a, **k):
            msg = next((x for x in a if isinstance(x, str)), "")
            _sent.append((kind, msg))
            return True if kind == "reply" else 1
        return f

    notifier.reply_line_message = rec("reply")
    notifier.send_line_message = rec("push")
    notifier.broadcast_line_message = rec("broadcast")
    notifier.send_line_with_quick_reply = rec("quick")
    notifier.broadcast_with_quick_reply = rec("quick-broadcast")
    # Matter（炊飯器の電源）も止める
    import src.lock_manager as lm

    async def _no_matter(*a, **k):
        _sent.append(("matter", "電源操作はスタブ"))
        return True

    lm._matter_set_power = _no_matter


def _use_db_copy() -> Path:
    """本番 DB のコピーに差し替える。probe が本番を書き換えないため。"""
    import src.db as db

    tmp = Path(tempfile.mkdtemp(prefix="line_probe_")) / "iot.db"
    shutil.copy2(ROOT / "data" / "iot.db", tmp)
    db.DB_PATH = tmp
    return tmp


def run_one(text: str, sender: str) -> None:
    import src.line_commands as lc

    _sent.clear()
    cmd = lc.match_command(text)
    reply = asyncio.run(lc.dispatch(text, sender))
    if isinstance(reply, dict):
        reply = f"[メニュー] {len(reply.get('items', []))}個のボタン"
    body = (reply or "").split("\n")[0][:52] if reply else ""
    if reply is None and not _sent:
        if cmd == "link":
            # link だけは dispatch の外。webhook が URL を組み立てて返す。
            from src.web.app import _build_url_reply
            body = "[webhook が返す] " + _build_url_reply().split("\n")[0][:38]
        elif cmd == "unknown":
            body = "[未知 → 案内] すみません、そのコマンドは分かりませんでした。"
        else:
            body = "（返信なし）"
    print(f"  {text:<16} {cmd:<18} {body}")
    for kind, msg in _sent:
        if kind != "reply":
            print(f"  {'':<16} {'':<18} └ {kind}: {msg.splitlines()[0][:44]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="*", help="送るコマンド")
    ap.add_argument("--all", action="store_true", help="主要コマンドを一括で試す")
    ap.add_argument("--postback", help="postback の data 文字列")
    ap.add_argument("--sender", default="", help="送信者ID（既定: 登録済みの先頭）")
    args = ap.parse_args()

    _use_db_copy()
    _install_stubs()

    import src.line_commands as lc
    from src.db import get_conn

    sender = args.sender
    if not sender:
        conn = get_conn()
        try:
            row = conn.execute("SELECT line_user_id FROM family_line_users LIMIT 1").fetchone()
        finally:
            conn.close()
        sender = row[0] if row else "Uprobe"

    print(f"  送信者: {sender[:8]}…（登録済み）  DB: コピー  LINE送信: スタブ")
    print(f"  {'入力':<16} {'判定':<18} 返信")
    print("  " + "-" * 74)

    if args.postback:
        _sent.clear()
        from src.web.app import app  # noqa: F401  （ハンドラの import 経路を合わせる）
        handlers = {
            "attribute:": lc.handle_attribute_postback,
            "merge:": lc.handle_merge_postback,
            "confirm:": lc.handle_confirm_postback,
            "rice_action:": lc.handle_rice_action_postback,
            "bath_cls:": lc.handle_bath_classification_postback,
            "lock_confirm:": lc.handle_lock_confirm_postback,
            "sess_confirm:": lc.handle_session_confirm_postback,
        }
        fn = next((f for p, f in handlers.items() if args.postback.startswith(p)), None)
        if fn is None:
            print(f"  {args.postback}: 対応するハンドラが無い（= 押しても無反応）")
            return 1
        reply = asyncio.run(fn(args.postback, sender))
        print(f"  {args.postback:<16} {'postback':<18} {(reply or '（返信なし）').splitlines()[0][:52]}")
        return 0

    targets = MAIN_COMMANDS if args.all or not args.text else [" ".join(args.text)]
    for t in targets:
        run_one(t, sender)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
