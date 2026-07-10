"""週次レポートをLINEに送信する。

過去7日間の統計を集計し、LINEで家族に送信する。
crontab 例:
  0 22 * * 0  cd ~/IoT && venv/bin/python scripts/weekly_report.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db, get_conn
from src.notifier import send_line_message
from src.settings import get_bool

GRANDMA_ID = 1
MEAL_LABELS = {"朝食", "昼食", "夕食", "間食", "おやつ"}
STAMP_LABELS = ["起床", "お薬", "朝食", "昼食", "お風呂", "夕食", "就寝"]


def _period_dates(days: int = 7) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def _meals_per_day(conn, dates: list[str]) -> dict[str, int]:
    result: dict[str, int] = {d: 0 for d in dates}
    placeholders = ",".join("?" * len(dates))
    # meal_sessions から食事セッションをカウント
    meal_list = ",".join(f"'{m}'" for m in MEAL_LABELS)
    rows = conn.execute(
        f"""SELECT DATE(started_at) as d, COUNT(*) as cnt FROM meal_sessions
            WHERE person_id = ?
            AND DATE(started_at) IN ({placeholders})
            AND label IN ({meal_list})
            GROUP BY DATE(started_at)""",
        (GRANDMA_ID, *dates),
    ).fetchall()
    for r in rows:
        result[r["d"]] = r["cnt"]
    return result


def _stamp_achievements(conn, dates: list[str]) -> dict[str, int]:
    """日ごとのスタンプ達成数を返す。"""
    result: dict[str, int] = {d: 0 for d in dates}
    placeholders = ",".join("?" * len(dates))
    rows = conn.execute(
        f"""SELECT date, done_count FROM daily_scores
            WHERE person_id = ? AND date IN ({placeholders})""",
        (GRANDMA_ID, *dates),
    ).fetchall()
    for r in rows:
        result[r["date"]] = r["done_count"]
    return result


def _medicine_rate(conn, dates: list[str]) -> tuple[int, int]:
    """お薬: (実績回数, 予定回数) を返す。"""
    sched_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM medicine_schedule WHERE enabled = 1"
    ).fetchone()["cnt"]
    placeholders = ",".join("?" * len(dates))
    taken = conn.execute(
        f"""SELECT COUNT(*) as cnt FROM events
            WHERE source IN ('family_report', 'tablet_report')
            AND event_type = 'お薬'
            AND DATE(started_at) IN ({placeholders})""",
        dates,
    ).fetchone()["cnt"]
    return taken, sched_count * len(dates)


def _bath_days(conn, dates: list[str]) -> int:
    """お風呂に入った日数（ラベル=お風呂のsession発生日）。"""
    placeholders = ",".join("?" * len(dates))
    row = conn.execute(
        f"""SELECT COUNT(DISTINCT DATE(started_at)) as cnt FROM meal_sessions
            WHERE person_id = ? AND label = 'お風呂'
            AND DATE(started_at) IN ({placeholders})""",
        (GRANDMA_ID, *dates),
    ).fetchone()
    return row["cnt"] if row else 0


def _lock_count(conn, dates: list[str]) -> int:
    """炊飯器自動ロック発動回数。"""
    placeholders = ",".join("?" * len(dates))
    row = conn.execute(
        f"""SELECT COUNT(*) as cnt FROM events
            WHERE source = 'lock_manager'
            AND event_type = 'auto_lock'
            AND DATE(started_at) IN ({placeholders})""",
        dates,
    ).fetchone()
    return row["cnt"] if row else 0


def build_report() -> str:
    conn = get_conn()
    try:
        dates = _period_dates(7)
        meals = _meals_per_day(conn, dates)
        stamps = _stamp_achievements(conn, dates)
        med_taken, med_total = _medicine_rate(conn, dates)
        bath = _bath_days(conn, dates)
        lock_count = _lock_count(conn, dates)
    finally:
        conn.close()

    meal_values = list(meals.values())
    meal_avg = sum(meal_values) / len(meal_values) if meal_values else 0
    meal_max = max(meal_values) if meal_values else 0
    overeat_days = sum(1 for c in meal_values if c >= 3)

    stamp_values = list(stamps.values())
    stamp_avg = sum(stamp_values) / len(stamp_values) if stamp_values else 0
    stamp_total_pct = int(100 * sum(stamp_values) / (len(STAMP_LABELS) * len(dates))) if dates else 0

    med_pct = int(100 * med_taken / med_total) if med_total else 0

    # 日ごとの食事回数を視覚化（最大5回まで）
    chart_lines = []
    for d, cnt in meals.items():
        bar = "🍚" * min(cnt, 5) + ("+" if cnt > 5 else "")
        dow = datetime.strptime(d, "%Y-%m-%d").strftime("%a")
        chart_lines.append(f"  {d[5:]} ({dow}): {bar or '—'} {cnt}回")

    start_date = dates[0]
    end_date = dates[-1]

    lines = [
        f"📊 週次レポート ({start_date[5:]}〜{end_date[5:]})",
        "",
        "🍴 食事回数",
        f"  平均 {meal_avg:.1f}回/日 · 最大 {meal_max}回",
        f"  食べ過ぎ日数(3回以上): {overeat_days}日",
        "",
        "🍚 日別詳細:",
    ]
    lines.extend(chart_lines)
    lines.extend([
        "",
        "⭐ スタンプ達成",
        f"  平均 {stamp_avg:.1f}/{len(STAMP_LABELS)} · 週全体 {stamp_total_pct}%",
        "",
        f"💊 お薬確認率: {med_pct}% ({med_taken}/{med_total}回)",
        f"🛁 お風呂日数: {bath}/{len(dates)}日",
        f"🔒 自動ロック発動: {lock_count}回",
    ])

    # 気になる傾向
    lines.append("")
    lines.append("💡 気になる点:")
    flagged = False
    if overeat_days >= 3:
        lines.append(f"  • 食べ過ぎの日が{overeat_days}日ありました")
        flagged = True
    if bath < len(dates) - 2:
        lines.append(f"  • お風呂を{len(dates) - bath}日スキップしています")
        flagged = True
    if med_total and med_pct < 70:
        lines.append(f"  • お薬の服用率が{med_pct}%と低めです")
        flagged = True
    if stamp_total_pct < 50:
        lines.append(f"  • スタンプ達成率が{stamp_total_pct}%と低めです")
        flagged = True
    if not flagged:
        lines.append("  • 特になし。安定した1週間でした 🌸")

    return "\n".join(lines)


def main():
    init_db()
    report = build_report()
    print(report)
    print()
    if not get_bool("notify_weekly_report_enabled"):
        print("週次レポート通知OFF → 送信スキップ")
        return
    success = send_line_message(report)
    print(f"LINE送信: {'成功' if success else '失敗'}")


if __name__ == "__main__":
    main()
