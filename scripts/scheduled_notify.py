"""定期LINE通知スクリプト。

cronで定期実行し、条件に応じて家族にLINE通知を送る。

crontab設定例:
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
    """お薬未服用チェック。"""
    sessions = sessions_today(GRANDMA_ID)
    labels = {s.get("label") for s in sessions}
    if "お薬" not in labels:
        now = datetime.now()
        h = now.hour
        if h >= 12:
            send_line_message(
                "💊 お薬リマインド\n"
                "祖母がまだお薬を飲んでいないようです。\n"
                "確認をお願いします。"
            )
            print("お薬リマインド送信")
        elif h >= 9:
            send_line_message(
                "💊 お薬チェック\n"
                "祖母がまだお薬を飲んでいません。\n"
                "声かけをお願いします。"
            )
            print("お薬チェック送信")
        else:
            print("まだ早い時間帯 → スキップ")
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


def main():
    parser = argparse.ArgumentParser(description="定期LINE通知")
    parser.add_argument("type", choices=["medicine", "bath", "summary"],
                        help="medicine: お薬チェック, bath: お風呂チェック, summary: 1日のまとめ")
    args = parser.parse_args()

    init_db()

    if args.type == "medicine":
        check_medicine()
    elif args.type == "bath":
        check_bath()
    elif args.type == "summary":
        daily_summary()


if __name__ == "__main__":
    main()
