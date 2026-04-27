"""LINE通知のマスタースイッチを切り替える。

使い方:
    python scripts/toggle_notifications.py off    # 全自動通知を停止
    python scripts/toggle_notifications.py on     # 通知を再開
    python scripts/toggle_notifications.py status # 現在の状態を表示

「off」状態でも以下は動作:
- LINEからのコマンド返信（リンク・状況・タスク等）
- ヘルスチェック等のフラグファイル管理（送信のみ抑止される）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.settings import get_setting, set_setting, list_settings


def show_status():
    print("📊 通知設定の現状")
    print("=" * 50)
    for s in list_settings():
        if s["key"].startswith("notify_"):
            on = s["value"] == "1"
            mark = "✅ ON " if on else "🔇 OFF"
            print(f"  {mark}  {s['key']}")
            print(f"        {s['description']}")
    print()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status"):
        print(__doc__)
        sys.exit(1)

    init_db()
    cmd = sys.argv[1]

    if cmd == "status":
        show_status()
        return

    new_value = "1" if cmd == "on" else "0"
    set_setting("notify_master_enabled", new_value)

    if cmd == "off":
        print("🔇 LINE通知マスタースイッチを OFF にしました。")
        print()
        print("✅ 影響範囲:")
        print("  - 食事検知/ロック/お薬/お風呂/まとめ/週次/異常検知/ヘルスチェック → 全停止")
        print("  - URL更新通知も停止")
        print()
        print("✅ 引き続き動作するもの:")
        print("  - LINEに送るコマンド（リンク・状況・タスク等）への返信")
        print("  - DBへのイベント記録、家族管理画面、タブレット表示")
        print()
        print("再開するには: python scripts/toggle_notifications.py on")
    else:
        print("🔊 LINE通知マスタースイッチを ON にしました。")
        print("各機能の個別設定は ⚙️ 詳細設定（家族UI）で調整できます。")


if __name__ == "__main__":
    main()
