"""未対応の pending_notifications の自動タイムアウト処理スクリプト。

ポリシー変更（2026-05-15）:
- LINE 再通知は廃止（深夜帯/翌日にズレた時刻の通知が届く問題のため）
- 未対応通知は **家族管理画面**で一覧表示・対応する方式に変更
- このスクリプトは「24時間以上経過した未対応通知を自動タイムアウト扱い」

追加ポリシー（2026-07-18）:
- session_confirm / attribute_session をタイムアウトする際は、単に auto_expired ではなく
  **時間帯 + 主要 person_id で meal_sessions を自動確定**する:
  - meal_sessions.label は sessions.py が既に時刻から設定済 (朝食/昼食/夕食/間食)
  - person_id が非0 (クラスタ生成時に推定済) ならそのまま採用
  - 0 (未確定) の場合、session_events から event.person_id を集計し
    3件以上で過半数を占める id があればそれ、なければ祖母 (id=1) を default
    (家に映る顔の 87% は祖母)
  - meal_sessions.confirmed=1, confirmed_by='auto_timeout_infer' で更新
  過去 7日で 71% の pending が家族未応答で放置されていた問題への対応。
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db, get_conn, transaction

TIMEOUT_HOURS = 24  # 24時間以上対応なしの通知は自動タイムアウト扱いに
GRANDMA_ID = 1  # 推定失敗時の default person_id


def _infer_person_for_session(conn, session_id: int) -> int:
    """session_events から event.person_id の最頻値を返す。
    3件以上 かつ過半数を占める id があればそれ、なければ祖母 (GRANDMA_ID) を default。
    """
    counts: Counter[int] = Counter()
    total = 0
    for r in conn.execute("""
        SELECT e.person_id FROM session_events se
        JOIN events e ON e.id = se.event_id
        WHERE se.session_id = ? AND e.person_id IS NOT NULL AND e.person_id > 0
    """, (session_id,)):
        counts[r["person_id"]] += 1
        total += 1
    if total >= 3 and counts:
        top_id, top_n = counts.most_common(1)[0]
        if top_n * 2 >= total:  # >= 50%
            return top_id
    return GRANDMA_ID


def _auto_confirm_session(conn, session_id: int) -> str:
    """meal_sessions を自動確定。戻り値は completed_action 用の説明文字列。"""
    row = conn.execute(
        "SELECT person_id, label, confirmed FROM meal_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not row:
        return f"{TIMEOUT_HOURS}時間応答なし → 自動タイムアウト (対象 session なし)"
    if row["confirmed"] == 1:
        return f"{TIMEOUT_HOURS}時間応答なし → 既に別経路で確定済み"

    if row["person_id"] and row["person_id"] > 0:
        inferred_person = row["person_id"]
        source = "cluster推定"
    else:
        inferred_person = _infer_person_for_session(conn, session_id)
        source = "events集計" if inferred_person != GRANDMA_ID else "default(祖母)"
    label = row["label"] or "食事"

    conn.execute("""
        UPDATE meal_sessions
           SET confirmed = 1,
               person_id = ?,
               confirmed_by = 'auto_timeout_infer',
               confirmed_at = CURRENT_TIMESTAMP
         WHERE id = ?
    """, (inferred_person, session_id))
    person_row = conn.execute("SELECT name FROM persons WHERE id=?", (inferred_person,)).fetchone()
    name_str = person_row["name"] if person_row else f"id={inferred_person}"
    return f"{TIMEOUT_HOURS}時間応答なし → 時刻+主要人物で自動確定 ({label} / {name_str} / {source})"


def main():
    init_db()
    # pending_notifications.last_notified_at / created_at は CURRENT_TIMESTAMP (UTC)
    cutoff = (datetime.utcnow() - timedelta(hours=TIMEOUT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    try:
        timeouts = conn.execute(
            """SELECT id, notification_type, context_key
                 FROM pending_notifications
                WHERE completed_at IS NULL
                  AND created_at < ?""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    if not timeouts:
        print("タイムアウト対象なし")
        return

    for t in timeouts:
        try:
            with transaction() as c:
                completed_action = None
                if t["notification_type"] in ("session_confirm", "attribute_session") \
                   and (t["context_key"] or "").startswith("session_"):
                    try:
                        sid = int(t["context_key"].split("_", 1)[1])
                        completed_action = _auto_confirm_session(c, sid)
                    except (ValueError, IndexError):
                        completed_action = None

                if not completed_action:
                    completed_action = f"{TIMEOUT_HOURS}時間応答なし → 自動タイムアウト"

                c.execute(
                    """UPDATE pending_notifications
                          SET completed_at = CURRENT_TIMESTAMP,
                              completed_by = 'auto_timeout',
                              completed_action = ?
                        WHERE id = ?""",
                    (completed_action, t["id"]),
                )
            print(f"タイムアウト: id={t['id']} type={t['notification_type']} ctx={t['context_key']}")
            print(f"  → {completed_action}")
        except Exception as e:
            print(f"タイムアウト処理失敗 id={t['id']}: {e}")


if __name__ == "__main__":
    main()
