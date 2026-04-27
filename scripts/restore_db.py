"""DBバックアップから復元する。

使い方:
    python scripts/restore_db.py                  # 利用可能なバックアップを一覧
    python scripts/restore_db.py --date 20260424  # 指定日付のバックアップから復元
    python scripts/restore_db.py --latest         # 最新のバックアップから復元
    python scripts/restore_db.py --file path.db   # ファイル指定で復元

復元前に現在DBは data/iot_pre_restore_<timestamp>.db に退避される。
復元後は systemd サービスを再起動すること。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "iot.db"
BACKUP_DIR = ROOT / "data" / "backup"


def list_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob("iot_*.db"), reverse=True)
    return files


def show_backups():
    backups = list_backups()
    if not backups:
        print("バックアップが見つかりません。")
        return
    print(f"利用可能なバックアップ ({len(backups)}件):")
    for f in backups:
        size = f.stat().st_size / 1024
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        # event count
        try:
            conn = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            conn.close()
            cnt_str = f"{cnt}件"
        except Exception:
            cnt_str = "?"
        print(f"  {f.name}  ({size:.1f}KB, {mtime}, events={cnt_str})")


def verify_backup(path: Path) -> tuple[bool, str]:
    """バックアップDBの整合性をチェック。"""
    if not path.exists():
        return False, "ファイルが存在しません"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            r = conn.execute("PRAGMA integrity_check").fetchone()
            if r[0] != "ok":
                return False, f"integrity_check: {r[0]}"
            cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            return True, f"events={cnt}"
        finally:
            conn.close()
    except Exception as e:
        return False, f"open失敗: {e}"


def restore(src: Path, *, force: bool = False) -> bool:
    if not src.exists():
        print(f"ERROR: {src} が存在しません")
        return False

    ok, msg = verify_backup(src)
    if not ok:
        print(f"ERROR: バックアップが不正です ({msg})")
        return False
    print(f"バックアップ確認OK: {msg}")

    if not force:
        ans = input(f"\n{src.name} から復元します。現在のDBは退避されます。続行しますか？ [y/N]: ")
        if ans.lower() != "y":
            print("中断しました")
            return False

    # サービス停止確認
    print("\n注意: 安全に復元するために以下のサービスを停止することを推奨:")
    print("  sudo systemctl stop iot-web iot-monitor iot-matter")
    if not force:
        ans = input("サービスは停止済みですか？ [y/N]: ")
        if ans.lower() != "y":
            print("中断しました")
            return False

    # 現DBを退避
    if DB_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_current = ROOT / "data" / f"iot_pre_restore_{ts}.db"
        shutil.copy2(DB_PATH, backup_current)
        print(f"現在DBを退避: {backup_current.name}")

    # 復元
    shutil.copy2(src, DB_PATH)
    print(f"✅ 復元完了: {src.name} → {DB_PATH.name}")
    print("\n次の手順:")
    print("  sudo systemctl start iot-matter iot-web iot-monitor")
    return True


def main():
    parser = argparse.ArgumentParser(description="DB復元")
    parser.add_argument("--date", help="日付指定 (YYYYMMDD)")
    parser.add_argument("--latest", action="store_true", help="最新のバックアップから復元")
    parser.add_argument("--file", help="ファイルパス指定で復元")
    parser.add_argument("--force", "-y", action="store_true", help="確認プロンプトを省略")
    args = parser.parse_args()

    if not (args.date or args.latest or args.file):
        show_backups()
        return

    if args.file:
        src = Path(args.file)
    elif args.latest:
        backups = list_backups()
        if not backups:
            print("バックアップが見つかりません")
            sys.exit(1)
        src = backups[0]
        print(f"最新: {src.name}")
    else:
        src = BACKUP_DIR / f"iot_{args.date}.db"

    if restore(src, force=args.force):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
