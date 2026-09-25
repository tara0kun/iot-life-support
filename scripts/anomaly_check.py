"""異常検知スクリプト。

以下をチェックしてLINE通知する:
  A. 深夜炊飯器稼働 (2:00-5:00 に rice_cooker power_on イベント)
  B. センサー無反応タイマー (日中 4時間以上どのセンサーも反応なし)
  C. 冷蔵庫開きっぱなし (fridge door open → close なしで30分経過)

crontab 例（10分おき）:
  */10 * * * *  cd ~/IoT && venv/bin/python scripts/anomaly_check.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db, get_conn
from src.notifier import send_line_message, send_actionable_notification
from src.settings import get_bool, get_int

FLAG_DIR = Path(__file__).resolve().parent.parent / "data" / "anomaly_flags"
FLAG_DIR.mkdir(parents=True, exist_ok=True)

DAYTIME_START = 7    # 時
DAYTIME_END = 22


def _already_notified_today(key: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    flag = FLAG_DIR / f"{key}_{today}"
    return flag.exists()


def _mark_notified(key: str):
    today = datetime.now().strftime("%Y-%m-%d")
    (FLAG_DIR / f"{key}_{today}").touch()


def _last_sensor_activity(conn) -> datetime | None:
    """祖母が家の中で動いた形跡の、最新時刻を返す。

    **「祖母が生きている証拠」だけを数える。** 以前はここに次の3つが混ざっており、
    その結果この安否アラートは構造的に発火不能だった:

      1. `camera`: person_detected が24時間ほぼ平坦に鳴り続けるノイズ
         （実測: 深夜3時でも110件/時、直近30日で全イベントの51%）。
         MAX() を取る以上、これが入っていると経過時間は常に数分で、
         何時間でも閾値に到達しない。
      2. `family_report` / `family_override`: 別居家族の遠隔操作。
         祖母の活動ではない。
      3. `pending_notifications.completed_at`: 家族がLINEのボタンを押した時刻。
         東京で家族がボタンを1つ押すだけで祖母の生存タイマーがリセットされていた。

    残したもの: 家の中で物理的に動かないと発生しないセンサーと、
    祖母自身がタブレットを押した記録 (tablet_report)。

    camera を戻す場合は person_id=1（祖母と顔識別できたもの）に限ること。
    """
    row = conn.execute(
        """SELECT MAX(latest) AS latest FROM (
             SELECT MAX(started_at) AS latest FROM events
              WHERE source IN (
                'rice_cooker', 'bath_door', 'bath_motion',
                'toilet_door', 'fridge', 'rice_cooker_lid',
                'tablet_report'
              )
             UNION ALL
             SELECT MAX(started_at) FROM events
              WHERE source='bathroom_meter' AND event_type IN ('shower_start','shower_end')
           )"""
    ).fetchone()
    if not row or not row["latest"]:
        return None
    latest = row["latest"]
    if isinstance(latest, str):
        try:
            latest = datetime.fromisoformat(latest.replace("T", " "))
        except ValueError:
            return None
    return latest


def check_inactivity():
    """日中にセンサーが長時間反応しない場合にアラート。"""
    if not get_bool("notify_anomaly_enabled"):
        print("異常検知OFF → スキップ")
        return
    # 既定を4→6時間に変更。camera を外したことで、この閾値は初めて実際に
    # 到達しうるようになった。センサー復旧後(2026-09-16〜)の実測では日中の
    # 無反応は最大4.1時間・99%tile 3.2時間で、4時間だと約10日に1回の誤報が出る。
    # 6時間なら測定期間で誤報ゼロ。settings の anomaly_inactivity_hours で上書き可。
    inactivity_hours = get_int("anomaly_inactivity_hours", 6)
    now = datetime.now()
    if not (DAYTIME_START <= now.hour < DAYTIME_END):
        print(f"時間外 ({now.hour}時) → スキップ")
        return
    if _already_notified_today("inactivity"):
        print("今日は通知済み → スキップ")
        return

    conn = get_conn()
    try:
        latest = _last_sensor_activity(conn)
    finally:
        conn.close()

    if latest is None:
        print("センサーイベントなし → データ不足でスキップ")
        return

    gap = now - latest
    if gap < timedelta(hours=inactivity_hours):
        print(f"最終活動 {gap.total_seconds()/60:.0f}分前 → 通常")
        return

    msg = (
        "⚠️ 安否確認アラート\n\n"
        f"直近{inactivity_hours}時間、センサーに反応がありません。\n"
        f"最終活動: {latest.strftime('%m/%d %H:%M')}\n\n"
        "様子を確認してください。"
    )
    today = datetime.now().strftime("%Y-%m-%d")
    send_actionable_notification("anomaly_inactivity", today, msg)
    _mark_notified("inactivity")
    print(f"無活動アラート送信（最終: {latest}）")


def check_night_rice():
    """深夜の炊飯器稼働をチェック。"""
    if not get_bool("notify_anomaly_enabled"):
        print("異常検知OFF → スキップ")
        return
    night_start = get_int("anomaly_night_rice_start_hour", 2)
    night_end = get_int("anomaly_night_rice_end_hour", 5)
    now = datetime.now()
    if not (night_start <= now.hour < night_end):
        print(f"深夜時間外 ({now.hour}時) → スキップ")
        return
    if _already_notified_today("night_rice"):
        print("今日は通知済み → スキップ")
        return

    conn = get_conn()
    try:
        since = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            """SELECT COUNT(*) as cnt, MAX(started_at) as latest FROM events
               WHERE source = 'rice_cooker'
               AND event_type = 'power_on'
               AND started_at >= ?""",
            (since,),
        ).fetchone()
    finally:
        conn.close()

    if not row or row["cnt"] == 0:
        print("深夜炊飯器稼働なし → 正常")
        return

    latest = row["latest"]
    msg = (
        "🚨 深夜の炊飯器稼働を検知\n\n"
        f"時刻: {latest}\n"
        "認知症の徘徊・異常行動の可能性があります。\n"
        "至急様子を確認してください。"
    )
    today = now.strftime("%Y-%m-%d")
    send_actionable_notification("anomaly_night_rice", today, msg)
    _mark_notified("night_rice")
    print(f"深夜炊飯器アラート送信（{latest}）")


def check_fridge_open():
    """冷蔵庫開きっぱなしチェック。"""
    if not get_bool("notify_anomaly_enabled"):
        print("異常検知OFF → スキップ")
        return
    threshold_min = get_int("anomaly_fridge_open_minutes", 30)
    if _already_notified_today("fridge_open"):
        print("今日は通知済み → スキップ")
        return

    now = datetime.now()
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT started_at, event_type FROM events
               WHERE source = 'fridge'
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
    finally:
        conn.close()

    if not row or row["event_type"] != "open":
        print("冷蔵庫は開いていない → 正常")
        return

    opened_at = row["started_at"]
    if isinstance(opened_at, str):
        try:
            opened_at = datetime.fromisoformat(opened_at.replace("T", " "))
        except ValueError:
            return

    gap = now - opened_at
    if gap < timedelta(minutes=threshold_min):
        print(f"冷蔵庫開放 {gap.total_seconds()/60:.0f}分 → 正常範囲")
        return

    msg = (
        "🧊 冷蔵庫が開きっぱなしです\n\n"
        f"{opened_at.strftime('%H:%M')} から {gap.total_seconds()/60:.0f}分経過\n\n"
        "食材が傷む可能性があります。閉めるようお伝えください。"
    )
    today = now.strftime("%Y-%m-%d")
    send_actionable_notification("anomaly_fridge_open", today, msg)
    _mark_notified("fridge_open")
    print(f"冷蔵庫アラート送信（{gap.total_seconds()/60:.0f}分経過）")


def main():
    parser = argparse.ArgumentParser(description="異常検知")
    parser.add_argument(
        "--type",
        choices=["all", "inactivity", "night_rice", "fridge"],
        default="all",
    )
    args = parser.parse_args()
    init_db()

    if args.type in ("all", "inactivity"):
        check_inactivity()
    if args.type in ("all", "night_rice"):
        check_night_rice()
    if args.type in ("all", "fridge"):
        check_fridge_open()


if __name__ == "__main__":
    main()
