"""定期LINE通知スクリプト。

cronで定期実行し、条件に応じて家族にLINE通知を送る。

crontab設定例:
  */30 7-22 * * *  cd ~/IoT && venv/bin/python scripts/scheduled_notify.py care_tasks  # 30分おきに担当通知
  0 9,12 * * *  cd ~/IoT && venv/bin/python scripts/scheduled_notify.py medicine
  0 18   * * *  cd ~/IoT && venv/bin/python scripts/scheduled_notify.py bath
  0 22   * * *  cd ~/IoT && venv/bin/python scripts/scheduled_notify.py summary
"""
import argparse
import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db, get_conn
from src.sessions import sessions_today
from src.notifier import send_line_message
from src.garden import save_daily_score, FLOWER_TYPES

GRANDMA_ID = 1


def check_medicine():
    """お薬未服用チェック（DBのスケジュールに基づく）。"""
    sessions = sessions_today(GRANDMA_ID)
    labels = {s.get("label") for s in sessions}
    if "お薬" not in labels:
        now = datetime.now()
        h = now.hour
        # DBからスケジュール取得
        conn = get_conn()
        try:
            schedules = conn.execute(
                "SELECT timing, hour FROM medicine_schedule WHERE enabled = 1"
            ).fetchall()
        finally:
            conn.close()
        if not schedules:
            print("薬スケジュール未設定 → スキップ")
            return
        # 現在時刻を過ぎているスケジュールがあれば通知
        for s in schedules:
            if h >= s["hour"]:
                delay = h - s["hour"]
                if delay >= 2:
                    send_line_message(
                        f"💊 お薬リマインド（{s['timing']}）\n"
                        f"祖母がまだ{s['timing']}のお薬を飲んでいないようです。\n"
                        "確認をお願いします。"
                    )
                    print(f"お薬リマインド送信（{s['timing']}）")
                else:
                    send_line_message(
                        f"💊 お薬チェック（{s['timing']}）\n"
                        f"祖母がまだ{s['timing']}のお薬を飲んでいません。\n"
                        "声かけをお願いします。"
                    )
                    print(f"お薬チェック送信（{s['timing']}）")
                return  # 1通知のみ
        print("まだ薬の時間前 → スキップ")
    else:
        print("お薬服用済み → スキップ")


def check_bath():
    """お風呂未入浴チェック（夕方に実行）。"""
    sessions = sessions_today(GRANDMA_ID)
    labels = {s.get("label") for s in sessions}
    if "お風呂" not in labels:
        send_line_message(
            "🛁 お風呂リマインド\n"
            "祖母がまだお風呂に入っていないようです。\n"
            "声かけをお願いします。"
        )
        print("お風呂リマインド送信")
    else:
        print("お風呂入浴済み → スキップ")


def daily_summary():
    """1日のまとめ通知（夜に実行）。"""
    sessions = sessions_today(GRANDMA_ID)
    labels = [s.get("label", "") for s in sessions]
    now = datetime.now()

    # スタンプ項目
    all_items = ["起床", "お薬", "朝食", "昼食", "お風呂", "夕食", "就寝"]
    done = [item for item in all_items if item in labels]
    not_done = [item for item in all_items if item not in labels]
    done_count = len(done)
    total = len(all_items)

    # お花の種類
    flower_idx = min(done_count, len(FLOWER_TYPES) - 1)
    flower = FLOWER_TYPES[flower_idx]

    # スコア保存
    save_daily_score(GRANDMA_ID, now.date(), done_count, total, {"done": done})

    # 食事回数
    meal_labels = {"朝食", "昼食", "夕食", "間食"}
    meal_count = len([l for l in labels if l in meal_labels])

    msg = f"📋 きょうのまとめ（{now.strftime('%m/%d %A')}）\n\n"
    msg += f"🌸 スコア: {done_count}/{total}  {flower['emoji']} {flower['label']}\n\n"

    if done:
        msg += "✅ できたこと:\n"
        msg += "　" + "、".join(done) + "\n\n"

    if not_done:
        msg += "⬜ まだのもの:\n"
        msg += "　" + "、".join(not_done) + "\n\n"

    msg += f"🍚 食事回数: {meal_count}回\n"

    if meal_count >= 3:
        msg += "⚠️ 食事が多めでした\n"

    # 祖母ボタンの検証状況
    conn = get_conn()
    try:
        today_start = datetime.combine(now.date(), time.min)
        tablet_events = conn.execute(
            """SELECT event_type, confidence, raw_meta FROM events
               WHERE source = 'tablet_report' AND person_id = ? AND started_at >= ?""",
            (GRANDMA_ID, today_start),
        ).fetchall()
    finally:
        conn.close()

    if tablet_events:
        verified = [e for e in tablet_events if e["confidence"] == 1.0]
        unverified = [e for e in tablet_events if e["confidence"] == 0.0]
        msg += f"\n📱 祖母ボタン: {len(tablet_events)}回押下\n"
        if verified:
            msg += "　✅ 確認済み: " + "、".join(e["event_type"] for e in verified) + "\n"
        if unverified:
            msg += "　❌ 未確認: " + "、".join(e["event_type"].replace("_unverified", "") for e in unverified) + "\n"

    send_line_message(msg)
    print("1日のまとめ送信")
    print(f"  スコア: {done_count}/{total} {flower['emoji']}")


def check_care_tasks():
    """家族タスクのリマインダー通知。

    各タスクの reminder_hour になったら担当者を通知。
    当該の時刻〜30分以内の1回だけ送る（重複防止）。
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT t.id, t.task_name, t.assignee_name, t.reminder_hour,
                      l.id as log_id
               FROM care_tasks t
               LEFT JOIN care_task_logs l ON l.task_id = t.id AND l.date = ?
               WHERE t.enabled = 1""",
            (today,),
        ).fetchall()
    finally:
        conn.close()

    sent = 0
    for r in rows:
        if r["log_id"] is not None:
            continue  # 既に完了済み
        rh = r["reminder_hour"]
        if rh is None:
            continue
        # reminder_hour と一致する時刻の、分=0〜29なら送る（30分cron想定）
        if now.hour != rh or now.minute >= 30:
            continue
        assignee = r["assignee_name"] or "未割当"
        msg = (
            f"🔔 タスクリマインダー\n\n"
            f"📋 {r['task_name']}\n"
            f"👤 担当: {assignee}\n"
            f"🕐 {rh:02d}:00〜\n\n"
            f"完了したら「済 {r['task_name']}」と返信してください。"
        )
        send_line_message(msg)
        sent += 1
        print(f"タスク通知送信: {r['task_name']} → {assignee}")
    if sent == 0:
        print("送信対象なし（時刻外 or 完了済み）")


def main():
    parser = argparse.ArgumentParser(description="定期LINE通知")
    parser.add_argument(
        "type",
        choices=["medicine", "bath", "summary", "care_tasks"],
        help="medicine: お薬, bath: お風呂, summary: 1日まとめ, care_tasks: 家族タスク",
    )
    args = parser.parse_args()

    init_db()

    if args.type == "medicine":
        check_medicine()
    elif args.type == "bath":
        check_bath()
    elif args.type == "summary":
        daily_summary()
    elif args.type == "care_tasks":
        check_care_tasks()


if __name__ == "__main__":
    main()
