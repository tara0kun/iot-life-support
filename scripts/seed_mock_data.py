"""モックデータをDBに投入して、システムの試運転を行う。

実際のセンサーを使わず、1日分の「祖母の典型的な1日」をシミュレートする。
タブレットUI・家族UIの表示確認、セッション集約、LINE通知テストに使える。

使い方:
    python scripts/seed_mock_data.py              # 今日のデータを生成
    python scripts/seed_mock_data.py --clear       # 既存データを消してから生成
    python scripts/seed_mock_data.py --scenario 2  # シナリオ2（食べ過ぎの日）
"""
import argparse
import sys
from datetime import datetime, timedelta, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import get_conn, init_db, transaction
from src.sessions import aggregate_sessions


def clear_today():
    today_start = datetime.combine(datetime.now().date(), time.min)
    with transaction() as conn:
        conn.execute("DELETE FROM session_events WHERE session_id IN (SELECT id FROM meal_sessions WHERE started_at >= ?)", (today_start,))
        conn.execute("DELETE FROM meal_sessions WHERE started_at >= ?", (today_start,))
        conn.execute("DELETE FROM events WHERE started_at >= ?", (today_start,))
    print("今日のデータを削除しました")


def clear_all():
    with transaction() as conn:
        conn.execute("DELETE FROM session_events")
        conn.execute("DELETE FROM meal_sessions")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM daily_scores")
    print("全データを削除しました")


# 対象日を保持するグローバル変数
_target_date = None

def t(hour, minute=0):
    """対象日の指定時刻のdatetimeを返す"""
    base = _target_date or datetime.now().date()
    return datetime.combine(base, time(hour, minute))


def insert_event(conn, person_id, source, event_type, at, value=None):
    conn.execute(
        """INSERT INTO events(person_id, source, event_type, started_at, value, confidence)
           VALUES(?, ?, ?, ?, ?, 1.0)""",
        (person_id, source, event_type, at, value),
    )


def scenario_normal(conn):
    """シナリオ1: 普通の1日（朝食・昼食・夕食、各1回ずつ）"""
    print("シナリオ1: 普通の1日")
    G = 1  # 祖母
    M = 2  # 母

    # === 朝 7:00〜7:30 祖母が朝食 ===
    insert_event(conn, G, "camera", "person_detected", t(6, 55), 1.0)
    insert_event(conn, G, "rice_cooker", "power_on", t(7, 0), 650.0)
    insert_event(conn, G, "contact_sensor", "open", t(7, 2))   # 冷蔵庫
    insert_event(conn, G, "contact_sensor", "close", t(7, 3))
    insert_event(conn, G, "rice_cooker", "power_off", t(7, 25), 2.0)
    insert_event(conn, G, "camera", "person_detected", t(7, 30), 1.0)

    # === 朝 7:40 母がキッチンを使う（祖母UIに出ない） ===
    insert_event(conn, M, "contact_sensor", "open", t(7, 40))
    insert_event(conn, M, "contact_sensor", "close", t(7, 41))
    insert_event(conn, M, "camera", "person_detected", t(7, 40), 1.0)

    # === 昼 12:00〜12:20 祖母が昼食 ===
    insert_event(conn, G, "camera", "person_detected", t(12, 0), 1.0)
    insert_event(conn, G, "contact_sensor", "open", t(12, 2))
    insert_event(conn, G, "contact_sensor", "close", t(12, 3))
    insert_event(conn, G, "ih", "power_on", t(12, 5), 800.0)
    insert_event(conn, G, "ih", "power_off", t(12, 15), 3.0)
    insert_event(conn, G, "contact_sensor", "open", t(12, 18))
    insert_event(conn, G, "contact_sensor", "close", t(12, 19))

    # === 夕 18:00〜18:30 祖母が夕食 ===
    now = datetime.now()
    if t(18, 0) < now:
        insert_event(conn, G, "camera", "person_detected", t(18, 0), 1.0)
        insert_event(conn, G, "rice_cooker", "power_on", t(18, 5), 620.0)
        insert_event(conn, G, "contact_sensor", "open", t(18, 8))
        insert_event(conn, G, "contact_sensor", "close", t(18, 9))
        insert_event(conn, G, "rice_cooker", "power_off", t(18, 28), 1.5)

    print(f"  朝食(7:00) + 昼食(12:00) + {'夕食(18:00)' if t(18,0) < now else '夕食は時間前'}")


def scenario_overeating(conn):
    """シナリオ2: 食べ過ぎの日（朝食後30分で再度食事行動）"""
    print("シナリオ2: 食べ過ぎの日（2回目の食事行動あり）")
    G = 1

    # === 朝食 7:00 ===
    insert_event(conn, G, "camera", "person_detected", t(7, 0), 1.0)
    insert_event(conn, G, "rice_cooker", "power_on", t(7, 0), 650.0)
    insert_event(conn, G, "contact_sensor", "open", t(7, 2))
    insert_event(conn, G, "contact_sensor", "close", t(7, 3))
    insert_event(conn, G, "rice_cooker", "power_off", t(7, 25), 2.0)

    # === 朝食後30分で再度冷蔵庫を開ける（問題行動） ===
    insert_event(conn, G, "camera", "person_detected", t(7, 55), 1.0)
    insert_event(conn, G, "contact_sensor", "open", t(7, 56))
    insert_event(conn, G, "contact_sensor", "close", t(7, 57))
    insert_event(conn, G, "contact_sensor", "open", t(7, 58))
    insert_event(conn, G, "contact_sensor", "close", t(7, 59))
    insert_event(conn, G, "rice_cooker", "power_on", t(8, 0), 640.0)
    insert_event(conn, G, "rice_cooker", "power_off", t(8, 20), 1.8)

    # === 昼食 12:00 ===
    insert_event(conn, G, "camera", "person_detected", t(12, 0), 1.0)
    insert_event(conn, G, "contact_sensor", "open", t(12, 5))
    insert_event(conn, G, "contact_sensor", "close", t(12, 6))
    insert_event(conn, G, "ih", "power_on", t(12, 8), 750.0)
    insert_event(conn, G, "ih", "power_off", t(12, 18), 2.0)

    # === 昼食後1時間でまた ===
    insert_event(conn, G, "camera", "person_detected", t(13, 10), 1.0)
    insert_event(conn, G, "contact_sensor", "open", t(13, 12))
    insert_event(conn, G, "contact_sensor", "close", t(13, 13))

    print("  朝食(7:00) + 2回目(7:55) + 昼食(12:00) + 間食試行(13:10)")


def scenario_with_toilet(conn):
    """シナリオ3: トイレ・入浴も含めた1日"""
    print("シナリオ3: 食事＋トイレ＋入浴の1日")
    G = 1

    # 起床
    insert_event(conn, G, "camera", "person_detected", t(6, 45), 1.0)

    # トイレ（朝）
    insert_event(conn, G, "toilet", "open", t(6, 50))
    insert_event(conn, G, "toilet", "close", t(6, 55))

    # 朝食
    insert_event(conn, G, "rice_cooker", "power_on", t(7, 5), 650.0)
    insert_event(conn, G, "contact_sensor", "open", t(7, 8))
    insert_event(conn, G, "contact_sensor", "close", t(7, 9))
    insert_event(conn, G, "rice_cooker", "power_off", t(7, 30), 2.0)

    # トイレ（午前）
    insert_event(conn, G, "toilet", "open", t(9, 30))
    insert_event(conn, G, "toilet", "close", t(9, 35))

    # 昼食
    insert_event(conn, G, "contact_sensor", "open", t(12, 0))
    insert_event(conn, G, "contact_sensor", "close", t(12, 1))
    insert_event(conn, G, "ih", "power_on", t(12, 5), 800.0)
    insert_event(conn, G, "ih", "power_off", t(12, 15), 2.5)

    # トイレ（午後）
    insert_event(conn, G, "toilet", "open", t(14, 0))
    insert_event(conn, G, "toilet", "close", t(14, 8))

    # 入浴
    now = datetime.now()
    if t(16, 0) < now:
        insert_event(conn, G, "bath_door", "close", t(16, 0))
        insert_event(conn, G, "bath_motion", "motion", t(16, 5))
        insert_event(conn, G, "bath_motion", "motion", t(16, 15))
        insert_event(conn, G, "bath_door", "open", t(16, 25))
        insert_event(conn, G, "bath_door", "bath_end", t(16, 25), 25.0)

    print(f"  朝食 + 昼食 + トイレ3回 + {'入浴あり' if t(16,0) < now else '入浴は時間前'}")


def scenario_full_day(conn):
    """シナリオ4: お花満開を目指す充実した1日（朝食+昼食+お風呂+夕食）"""
    print("シナリオ4: 充実した1日（お花の成長を確認）")
    G = 1

    # 起床 6:30
    insert_event(conn, G, "camera", "person_detected", t(6, 30), 1.0)

    # 朝食 7:00
    insert_event(conn, G, "rice_cooker", "power_on", t(7, 0), 650.0)
    insert_event(conn, G, "contact_sensor", "open", t(7, 3))
    insert_event(conn, G, "contact_sensor", "close", t(7, 4))
    insert_event(conn, G, "rice_cooker", "power_off", t(7, 25), 2.0)

    # 昼食 12:00
    insert_event(conn, G, "contact_sensor", "open", t(12, 0))
    insert_event(conn, G, "contact_sensor", "close", t(12, 1))
    insert_event(conn, G, "ih", "power_on", t(12, 5), 800.0)
    insert_event(conn, G, "ih", "power_off", t(12, 15), 2.5)

    # お風呂 16:00
    now = datetime.now()
    if t(16, 0) < now:
        insert_event(conn, G, "bath_door", "close", t(16, 0))
        insert_event(conn, G, "bath_motion", "motion", t(16, 10))
        insert_event(conn, G, "bath_motion", "motion", t(16, 20))
        insert_event(conn, G, "bath_door", "open", t(16, 30))
        insert_event(conn, G, "bath_door", "bath_end", t(16, 30), 30.0)

    # 夕食 18:00
    if t(18, 0) < now:
        insert_event(conn, G, "rice_cooker", "power_on", t(18, 0), 620.0)
        insert_event(conn, G, "contact_sensor", "open", t(18, 5))
        insert_event(conn, G, "contact_sensor", "close", t(18, 6))
        insert_event(conn, G, "rice_cooker", "power_off", t(18, 25), 1.5)

    done = "朝食 + 昼食"
    if t(16, 0) < now:
        done += " + お風呂"
    if t(18, 0) < now:
        done += " + 夕食"
    print(f"  {done}")


def scenario_demo_full(conn):
    """シナリオ5(demo): 家族デモ用 — 全機能が「動いている感」を見せるリアルな1日。

    - 食事/トイレ/入浴のセンサーイベント
    - 祖母の「できた」ボタン記録（センサー検証済）
    - 家族の証言記録（family_report）
    - 家族タスクの完了ログ
    - 家族からタブレットへの伝言（active prompt 1件）
    - 自動ロック発動 1回（夕食後）
    """
    print("シナリオ5: 家族デモ用（全機能の活動が見える1日）")
    G = 1  # 祖母
    M = 2  # 母
    import json as _json

    # 起床 6:30
    insert_event(conn, G, "camera", "person_detected", t(6, 30), 1.0)

    # 起床ボタン押下（センサー確認済として記録）
    conn.execute(
        """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
           VALUES(?, 'tablet_report', '起床', ?, NULL, 1.0, ?)""",
        (G, t(6, 35), _json.dumps({"verified": True, "verify_reason": "sensor_confirmed"}, ensure_ascii=False)),
    )
    conn.execute(
        """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
           VALUES(?, ?, ?, 1, '起床')""",
        (G, t(6, 35), t(6, 35)),
    )

    # トイレ
    insert_event(conn, G, "toilet", "open", t(6, 40))
    insert_event(conn, G, "toilet", "close", t(6, 47))

    # お薬（家族が証人として記録）
    conn.execute(
        """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
           VALUES(?, 'family_report', 'お薬', ?, NULL, 1.0, ?)""",
        (G, t(8, 5), _json.dumps({"witness": "母"}, ensure_ascii=False)),
    )
    conn.execute(
        """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
           VALUES(?, ?, ?, 1, 'お薬')""",
        (G, t(8, 5), t(8, 5)),
    )

    # 朝食 7:00
    insert_event(conn, G, "rice_cooker", "power_on", t(7, 0), 1100.0)
    insert_event(conn, G, "contact_sensor", "open", t(7, 3))
    insert_event(conn, G, "contact_sensor", "close", t(7, 4))
    insert_event(conn, G, "rice_cooker", "power_off", t(7, 35), 15.0)

    # トイレ午前
    insert_event(conn, G, "toilet", "open", t(10, 0))
    insert_event(conn, G, "toilet", "close", t(10, 5))

    # 昼食 12:00
    insert_event(conn, G, "contact_sensor", "open", t(12, 0))
    insert_event(conn, G, "contact_sensor", "close", t(12, 1))
    insert_event(conn, G, "ih", "power_on", t(12, 5), 850.0)
    insert_event(conn, G, "ih", "power_off", t(12, 18), 2.5)

    # 午後の散歩・帰宅 (camera person_detected)
    insert_event(conn, G, "camera", "person_detected", t(15, 30), 1.0)

    # お風呂 16:30
    now = datetime.now()
    if t(16, 30) < now:
        insert_event(conn, G, "bath_door", "close", t(16, 30))
        insert_event(conn, G, "bath_motion", "motion", t(16, 35))
        insert_event(conn, G, "bath_motion", "motion", t(16, 45))
        insert_event(conn, G, "bath_door", "open", t(17, 0))
        insert_event(conn, G, "bath_door", "bath_end", t(17, 0), 30.0)
        # お風呂後にボタン押下
        conn.execute(
            """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
               VALUES(?, 'tablet_report', 'お風呂', ?, NULL, 1.0, ?)""",
            (G, t(17, 5), _json.dumps({"verified": True, "verify_reason": "sensor_confirmed"}, ensure_ascii=False)),
        )
        conn.execute(
            """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
               VALUES(?, ?, ?, 1, 'お風呂')""",
            (G, t(17, 5), t(17, 5)),
        )

    # 夕食 18:00
    if t(18, 0) < now:
        insert_event(conn, G, "camera", "person_detected", t(18, 0), 1.0)
        insert_event(conn, G, "rice_cooker", "power_on", t(18, 5), 1100.0)
        insert_event(conn, G, "contact_sensor", "open", t(18, 8))
        insert_event(conn, G, "contact_sensor", "close", t(18, 9))
        insert_event(conn, G, "rice_cooker", "power_off", t(18, 35), 15.0)

    # 夕食後30分で再度食事行動（自動ロック発動）
    if t(19, 5) < now:
        insert_event(conn, G, "camera", "person_detected", t(19, 5), 1.0)
        insert_event(conn, G, "contact_sensor", "open", t(19, 6))
        insert_event(conn, G, "contact_sensor", "close", t(19, 7))
        # 自動ロック発動イベント
        conn.execute(
            """INSERT INTO events(person_id, source, event_type, started_at, raw_meta)
               VALUES(NULL, 'lock_manager', 'auto_lock', ?, ?)""",
            (t(19, 8), _json.dumps({"device": "rice_cooker", "reason": "食事2回目検知"}, ensure_ascii=False)),
        )

    # 就寝
    if t(21, 30) < now:
        insert_event(conn, G, "camera", "person_detected", t(21, 30), 1.0)
        conn.execute(
            """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
               VALUES(?, 'family_report', '就寝', ?, NULL, 1.0, ?)""",
            (G, t(21, 35), _json.dumps({"witness": "母"}, ensure_ascii=False)),
        )
        conn.execute(
            """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
               VALUES(?, ?, ?, 1, '就寝')""",
            (G, t(21, 35), t(21, 35)),
        )

    # 家族タスク完了（朝のお薬確認 - 母が完了）
    today = (_target_date or datetime.now().date()).strftime("%Y-%m-%d")
    conn.execute(
        """INSERT OR IGNORE INTO care_task_logs(task_id, date, done_by)
           SELECT id, ?, '母（家族デモ）' FROM care_tasks WHERE task_name LIKE '%朝のお薬%' LIMIT 1""",
        (today,),
    )

    # 家族からの伝言（active）
    expires = (datetime.now() + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO family_prompts(message, sent_by, created_at, expires_at)
           VALUES('夕方に電話するね', '母', ?, ?)""",
        (created, expires),
    )

    print("  起床/お薬(母)/朝食/昼食/お風呂/夕食/2回目検知→自動ロック/就寝(母)")
    print("  + タブレット記録2件 + 家族証言2件 + タスク完了 + 伝言1件")


def main():
    global _target_date
    parser = argparse.ArgumentParser(description="モックデータ投入")
    parser.add_argument("--clear", action="store_true", help="今日のデータを消してから投入")
    parser.add_argument("--clear-all", action="store_true", help="全データを消してから投入")
    parser.add_argument("--scenario", type=int, default=1, choices=[1, 2, 3, 4, 5],
                        help="1:普通, 2:食べ過ぎ, 3:トイレ+入浴, 4:充実, 5:家族デモ用")
    parser.add_argument("--days", type=int, default=1,
                        help="過去N日分のデータを生成（シナリオをローテーション）")
    args = parser.parse_args()

    init_db()

    if args.clear_all:
        clear_all()
    elif args.clear:
        clear_today()

    scenarios = [scenario_normal, scenario_overeating, scenario_with_toilet, scenario_full_day, scenario_demo_full]

    if args.days > 1:
        # 複数日分: 今日から過去N日分を生成
        for i in range(args.days - 1, -1, -1):
            _target_date = (datetime.now() - timedelta(days=i)).date()
            scenario_fn = scenarios[(args.days - 1 - i) % len(scenarios)]
            print(f"\n--- {_target_date} ---")
            with transaction() as conn:
                scenario_fn(conn)
            aggregate_sessions()
        _target_date = None
    else:
        # 単日
        _target_date = datetime.now().date()
        with transaction() as conn:
            if args.scenario == 1:
                scenario_normal(conn)
            elif args.scenario == 2:
                scenario_overeating(conn)
            elif args.scenario == 3:
                scenario_with_toilet(conn)
            elif args.scenario == 4:
                scenario_full_day(conn)
            elif args.scenario == 5:
                scenario_demo_full(conn)
        aggregate_sessions()

    # 結果表示
    conn = get_conn()
    total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    total_sessions = conn.execute("SELECT COUNT(*) FROM meal_sessions").fetchone()[0]
    conn.close()
    print(f"\n結果: イベント{total_events}件, セッション{total_sessions}件")
    print(f"タブレット確認: http://localhost:8000/tablet")
    print(f"家族画面確認:   http://localhost:8000/family")


if __name__ == "__main__":
    main()
