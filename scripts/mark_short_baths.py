"""短すぎる過去の「お風呂」セッションを却下済み(confirmed=-1)にする。

ドアを閉めて数秒で開ける「通過」が入浴として記録されていた。
2026-10-23 の実測では、新規に作られる入浴セッションの49%が60秒未満だった。
家族ダッシュボードと週次レポートの入浴回数が倍近く膨らんでいる。

今後の分は src/sessions.py の BATH_MIN_DURATION_SECONDS で作られなくなる。
このスクリプトは、それ以前に記録された分を遡って却下扱いにする。

**行は消さない。** 家族が誤検知として却下したときと同じ confirmed=-1 を立てる
だけなので、生イベントも残り、間違っていたら戻せる。

使い方:
    venv/bin/python scripts/mark_short_baths.py --dry-run   # 件数だけ見る
    venv/bin/python scripts/mark_short_baths.py --execute   # 実際に印をつける
    venv/bin/python scripts/mark_short_baths.py --revert    # 元に戻す
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import get_conn, transaction  # noqa: E402
from src.sessions import BATH_MIN_DURATION_SECONDS  # noqa: E402

MARKER = "auto_too_short"


def _targets(conn, threshold_sec: int) -> list[dict]:
    """しきい値未満で、まだ却下されていないお風呂セッション。"""
    rows = conn.execute(
        "SELECT id, started_at, ended_at, confirmed FROM meal_sessions "
        "WHERE label = 'お風呂' AND confirmed <> -1 ORDER BY started_at"
    ).fetchall()
    out = []
    for r in rows:
        try:
            dur = (
                datetime.fromisoformat(str(r["ended_at"]))
                - datetime.fromisoformat(str(r["started_at"]))
            ).total_seconds()
        except (TypeError, ValueError):
            continue  # 時刻が壊れている行は触らない
        if dur < threshold_sec:
            out.append({"id": r["id"], "started_at": r["started_at"], "sec": dur})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="件数だけ表示")
    g.add_argument("--execute", action="store_true", help="confirmed=-1 を立てる")
    g.add_argument("--revert", action="store_true", help="このスクリプトが立てた印を戻す")
    ap.add_argument("--seconds", type=int, default=BATH_MIN_DURATION_SECONDS,
                    help=f"しきい値（秒、既定 {BATH_MIN_DURATION_SECONDS}）")
    args = ap.parse_args()

    if args.revert:
        with transaction() as conn:
            cur = conn.execute(
                "UPDATE meal_sessions SET confirmed = 0, confirmed_by = NULL, "
                "confirmed_at = NULL WHERE label = 'お風呂' AND confirmed = -1 "
                "AND confirmed_by = ?", (MARKER,)
            )
            print(f"戻した件数: {cur.rowcount}")
        return 0

    conn = get_conn()
    try:
        targets = _targets(conn, args.seconds)
        total = conn.execute(
            "SELECT COUNT(*) FROM meal_sessions WHERE label='お風呂'"
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"しきい値: {args.seconds}秒未満")
    print(f"お風呂セッション全体: {total}件")
    print(f"該当（却下対象）: {len(targets)}件")
    if total:
        print(f"残る件数: {total - len(targets)}件")
    if targets:
        secs = sorted(t["sec"] for t in targets)
        print(f"該当の長さ: 最短 {secs[0]:.0f}秒 / 最長 {secs[-1]:.0f}秒")

    if args.dry_run:
        print("\n--dry-run のため変更なし。実行するには --execute")
        return 0

    if not targets:
        print("対象なし。終了。")
        return 0

    # バックアップの存在を確認する（prune_old_events.py と同じ作法）
    backup_dir = ROOT / "data" / "backup"
    latest = sorted(backup_dir.glob("iot_*.db"))[-1:] if backup_dir.exists() else []
    if not latest:
        print("⚠️ data/backup/ にバックアップが見当たりません。中止")
        return 1
    print(f"最新バックアップ: {latest[0].name}")

    ids = [t["id"] for t in targets]
    with transaction() as conn:
        ph = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE meal_sessions SET confirmed = -1, confirmed_by = ?, "
            f"confirmed_at = CURRENT_TIMESTAMP WHERE id IN ({ph})",
            (MARKER, *ids),
        )
    print(f"✅ {cur.rowcount}件を却下扱い(confirmed=-1)にした")
    print("   戻すには: venv/bin/python scripts/mark_short_baths.py --revert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
