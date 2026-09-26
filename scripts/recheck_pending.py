"""未対応の pending_notifications の自動タイムアウト処理スクリプト。

ポリシー変更（2026-05-15）:
- LINE 再通知は廃止（深夜帯/翌日にズレた時刻の通知が届く問題のため）
- 未対応通知は **家族管理画面**で一覧表示・対応する方式に変更
- このスクリプトは「24時間以上経過した未対応通知を自動タイムアウト扱い」

追加ポリシー（2026-09-25）:
- **CRITICAL_CATEGORIES のみ再通知を復活**。2026-05-15 に再通知を全廃したが、
  代替として用意した家族UIの未対応リストが 2026-09-03 以降ほぼ使われなくなり、
  9月の bath_emergency 8件中6件・long_toilet_stay 7件中5件が誰にも応答されないまま
  24時間後に無言で auto_timeout された（DB実測）。転倒疑いを1通鳴らして終わりにはできない。
- 廃止理由だった「深夜帯/翌日にズレた通知」は次の2点で再発を防ぐ:
    1. 再通知は初回から最大30分以内で打ち切る（15分間隔 × 最大2回）。翌日にズレようがない
    2. notifier._should_suppress_for_night() を通すので、深夜帯は NIGHT_ALLOWED のみ鳴る
- 打ち切り時は無言で閉じず「⏰ 誰も応答しませんでした」を全家族に broadcast する。
  誰も見ていなかったという事実だけは必ず残す。
- 非CRITICAL（session_confirm 等）は従来どおり再通知せず、24時間で自動タイムアウト。

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
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db, get_conn, transaction
from src.lock_manager import release_expired_locks
from src.notifier import (
    broadcast_line_message,
    broadcast_with_quick_reply,
    is_critical_category,
    _should_suppress_for_night,
)

TIMEOUT_HOURS = 24  # 24時間以上対応なしの通知は自動タイムアウト扱いに
GRANDMA_ID = 1  # 推定失敗時の default person_id

# --- 緊急系の再通知（CRITICAL_CATEGORIES のみ） ---
ESCALATE_AFTER_MINUTES = 15   # 最後の通知からこの分数を超えて未応答なら再通知
MAX_NOTIFY_COUNT = 3          # 初回を含む総通知数の上限。超えたら打ち切る
# 事象発生からこの分数を超えた通知は、もう再通知しない。
# cron停止・再起動・本機能の導入直後などに溜まった古い pending を掘り起こして
# 「今ごろ届く昔の通知」を家族に送らないための上限（2026-05-15 の廃止理由そのもの）。
MAX_ESCALATE_AGE_MINUTES = 60

# **事後報告型のカテゴリは再通知しない。**
# long_toilet_stay は monitor.py の toilet_door close ハンドラからしか発火せず、
# 「open からの滞在時間」を close の瞬間に計算して送る。つまり本人が
# ドアを開けて出てきた後にしか鳴らない。実測: 全53件のうち51件が
# toilet_door の close から ±1秒以内の発報。
# 倒れた人はドアを開けないので、この通知は「転倒の検知」ではなく
# 「長居していたが無事に出てきた」の事後報告である。
# 事後報告を再通知しても家族にできることは何もなく、2026-09-26 10:46 の件では
# 既に解決した事象について20通(5人×4回)を送ってしまった。
# 検知そのものを「滞在中に発火する」設計に直すまで、再通知対象から外す。
RETROSPECTIVE_CATEGORIES = {"long_toilet_stay"}
# 打ち切り通知（「⏰ 誰も応答しませんでした」）だけは、もう少し長く猶予を持たせる。
# 再通知2回が終わるのは発生から 35〜40分で、打ち切りは 55〜60分に落ちる。
# 上限が 60 分だと cron の tick が1回ズレただけで打ち切りが恒久的に消え、
# 「誰も見ていなかった事実だけは必ず残す」という約束が破れる（実測で
# journalctl 上 5014 起動中4回の欠落あり）。打ち切りは再通知と違って
# 1通だけの事後報告なので、多少遅れて届いても混乱しない（本文に発生時刻を入れる）。
MAX_GIVEUP_AGE_MINUTES = 180


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


def _utc_now() -> datetime:
    """pending_notifications の CURRENT_TIMESTAMP(UTC) と比較するための現在時刻。"""
    return datetime.now(timezone.utc)


def _parse_utc(ts: str) -> datetime:
    """SQLite の CURRENT_TIMESTAMP 文字列 (UTC, naive) を aware datetime にする。"""
    text = (ts or "").replace("T", " ").split(".")[0]
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _first_line(message: str) -> str:
    """LINE本文の1行目だけを取り出す（打ち切り通知の要約用）。"""
    for line in (message or "").splitlines():
        line = line.strip()
        if line:
            return line[:60]
    return "(本文なし)"


def escalate_unanswered() -> None:
    """未応答の CRITICAL 通知を再通知し、上限に達したら打ち切りを broadcast する。

    非CRITICAL には一切触れない（従来どおり24時間で無言タイムアウト）。
    """
    now = _utc_now()
    cutoff = (now - timedelta(minutes=ESCALATE_AFTER_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, notification_type, context_key, message,
                      quick_reply_json, notify_count, created_at
                 FROM pending_notifications
                WHERE completed_at IS NULL
                  AND COALESCE(last_notified_at, created_at) < ?
                ORDER BY id""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        category = r["notification_type"]
        if not is_critical_category(category):
            continue  # 雑務系は再通知しない（2026-05-15 のポリシーを維持）

        if category in RETROSPECTIVE_CATEGORIES:
            continue  # 事後報告。再通知しても家族にできることがない

        # 深夜帯は NIGHT_ALLOWED_CATEGORIES 以外を鳴らさない。
        # 「翌朝まで持ち越して変な時刻に届く」のを防ぐため、持ち越さずスキップする。
        if _should_suppress_for_night(category):
            print(f"再通知スキップ(深夜帯): id={r['id']} type={category}")
            continue

        created = _parse_utc(r["created_at"])
        age_min = (now - created).total_seconds() / 60
        jst = created + timedelta(hours=9)  # created_at は UTC 保存

        # `or 1` にすると notify_count=0（1通も送れていない）が 1 に化ける
        count = r["notify_count"] if r["notify_count"] is not None else 1

        if count > MAX_NOTIFY_COUNT:
            continue  # 打ち切り通知は送信済み。あとは24時間タイムアウトに任せる

        if count == MAX_NOTIFY_COUNT:
            if age_min > MAX_GIVEUP_AGE_MINUTES:
                print(f"打ち切り通知スキップ(発生から{age_min/60:.1f}時間経過): id={r['id']}")
                continue
            give_up = (
                f"⏰ 誰も応答しませんでした\n"
                f"{jst.strftime('%H:%M')}ごろの件: {_first_line(r['message'])}\n\n"
                f"{MAX_NOTIFY_COUNT}回お知らせしましたが反応がありませんでした。"
                f"心配な場合は直接ご確認ください。\n"
                f"（この確認は家族ページの「未対応の確認」に残っています）"
            )
            try:
                sent = broadcast_line_message(give_up)
            except Exception as e:
                print(f"打ち切り通知の送信失敗 id={r['id']}: {e}")
                continue
            if sent <= 0:
                # 1通も出せていないなら「通知済み」にしない。次の tick で再試行する。
                print(f"打ち切り通知の送信先なし: id={r['id']}")
                continue
            # **completed_at は埋めない。** 埋めると /api/pending-notifications の
            # `WHERE completed_at IS NULL` から外れ、家族ダッシュボードの
            # 「📨 未対応の確認」から約45分で消えてしまう。204e7af が
            # LINE再通知を廃止したときの代替がその一覧なので、LINE を諦めた
            # からといって一覧からも消すのは本末転倒。従来どおり24時間タイムアウトで閉じる。
            with transaction() as c:
                c.execute(
                    """UPDATE pending_notifications
                          SET notify_count = ?
                        WHERE id = ?""",
                    (MAX_NOTIFY_COUNT + 1, r["id"]),
                )
            print(f"打ち切り通知: id={r['id']} type={category} (送信{sent}件・一覧には残す)")
            continue

        # 以降は再通知。こちらは「今ごろ届く古い通知」を避けるため厳しめの上限。
        if age_min > MAX_ESCALATE_AGE_MINUTES:
            print(f"再通知スキップ(発生から{age_min/60:.1f}時間経過): id={r['id']} type={category}")
            continue

        # 再通知。何回目かを明示して「同じ通知が繰り返し来ている」と分かるようにする。
        try:
            items = json.loads(r["quick_reply_json"] or "[]")
        except Exception:
            items = []
        body = f"🔁 まだ確認されていません（{count + 1}/{MAX_NOTIFY_COUNT}回目）\n\n{r['message']}"
        try:
            sent = broadcast_with_quick_reply(body, items)
        except Exception as e:
            print(f"再通知の送信失敗 id={r['id']}: {e}")
            continue
        if sent <= 0:
            print(f"再通知の送信先なし: id={r['id']}")
            continue
        with transaction() as c:
            c.execute(
                """UPDATE pending_notifications
                      SET last_notified_at = CURRENT_TIMESTAMP,
                          notify_count = notify_count + 1
                    WHERE id = ?""",
                (r["id"],),
            )
        print(f"再通知: id={r['id']} type={category} → {count + 1}回目 (送信{sent}件)")


def main():
    init_db()

    # 先に緊急系の再通知/打ち切りを処理する（24時間タイムアウトより手前の時間スケール）
    escalate_unanswered()

    # ロック期限切れの自動解除。in-process の sleep ではなく cron に置くことで、
    # iot-monitor の再起動や停電をまたいでも解除が生き残る。
    try:
        released = asyncio.run(release_expired_locks())
        for name in released:
            print(f"ロック期限切れ → 自動解除: {name}")
    except Exception as e:
        print(f"ロック期限解除に失敗: {e}")

    # pending_notifications.last_notified_at / created_at は CURRENT_TIMESTAMP (UTC)
    cutoff = (_utc_now() - timedelta(hours=TIMEOUT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

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
