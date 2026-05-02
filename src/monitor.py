"""全センサーを統合起動し、イベントをDBに記録するメインループ。

使い方:
    python -m src.monitor
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sensors.matter_plug import MatterPlugMonitor, MatterPlugConfig, PlugReading
from src.sensors.contact_sensor import ContactSensorMonitor, ContactSensorConfig, ContactEvent
from src.sensors.camera import CameraMonitor, CameraConfig, CameraFrame
from src import event_bus
from src.sessions import aggregate_sessions, sessions_today
from src.lock_manager import lock_device, should_warn_recent_meal
from src.notifier import notify_meal_alert, notify_device_locked, send_line_message
from src.bath_monitor import BathMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).resolve().parent.parent / "logs" / "monitor.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("monitor")


def _load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


# --- コールバック: センサー → イベントバス ---

# power_readingは毎ポーリングで呼ばれるのでDBには書かない（ログのみ）
async def on_plug_reading(name: str, r: PlugReading) -> None:
    log.debug("[%s] %.1fW", name, r.power_w)


async def on_plug_start(name: str, r: PlugReading) -> None:
    await event_bus.record_event(
        source=name,
        event_type="power_on",
        value=r.power_w,
    )


async def on_plug_stop(name: str, r: PlugReading) -> None:
    await event_bus.record_event(
        source=name,
        event_type="power_off",
        value=r.power_w,
    )


_bath_monitor: BathMonitor | None = None

# T110のエイリアスで設置場所を判定（Tapoアプリでリネームする想定）
BATH_DOOR_ALIASES = {"浴室ドア", "bath_door", "風呂ドア"}
BATH_MOTION_ALIASES = {"脱衣所", "bath_motion", "脱衣所モーション"}


async def on_contact_change(event: ContactEvent) -> None:
    alias = event.alias
    # 浴室ドアセンサーの場合 → BathMonitor に委譲
    if alias in BATH_DOOR_ALIASES and _bath_monitor:
        if event.is_open:
            await _bath_monitor.door_opened()
        else:
            await _bath_monitor.door_closed()

    source_map = {
        "Tapo T110": "contact_sensor",
    }
    # エイリアスから分かりやすいソース名に変換
    if alias in BATH_DOOR_ALIASES:
        source = "bath_door"
    elif alias in BATH_MOTION_ALIASES:
        source = "bath_motion"
    else:
        source = source_map.get(alias, alias)
    await event_bus.record_event(
        source=source,
        event_type="open" if event.is_open else "close",
    )


async def on_motion_detected(event: ContactEvent) -> None:
    """T100モーションセンサー: 脱衣所の動き検知。"""
    alias = event.alias
    if alias in BATH_MOTION_ALIASES and _bath_monitor:
        await _bath_monitor.motion_detected()
    await event_bus.record_event(
        source="bath_motion" if alias in BATH_MOTION_ALIASES else alias,
        event_type="motion",
    )


_last_person_detection = 0.0

async def on_person_detected(frame: CameraFrame) -> None:
    import time
    global _last_person_detection
    now = time.time()
    # 30秒以内の連続検知は無視（DB肥大化防止）
    if now - _last_person_detection < 30:
        return
    _last_person_detection = now
    await event_bus.record_event(
        source="camera",
        event_type="person_detected",
        value=float(frame.face_count),
    )


# --- セッション集約 + ロック/通知 ---

GRANDMA_ID = 1
RICE_COOKER_NODE_ID = 1
MAX_MEALS_BEFORE_ALERT = 3

PREV_SESSION_LOOKUP_MINUTES = 90  # 「前と同じ食事」ボタン提示の上限


def _find_previous_meal_session(conn, before_time: datetime, exclude_session_id: int) -> dict | None:
    """新セッションの直前に発生した食事セッションを返す（PREV_SESSION_LOOKUP_MINUTES以内）。

    自動統合（60分）で漏れた60〜90分前の食事セッションを「前と同じ食事」候補として返す。
    """
    cutoff = before_time - timedelta(minutes=PREV_SESSION_LOOKUP_MINUTES)
    row = conn.execute(
        """SELECT id, person_id, started_at, label
             FROM meal_sessions
            WHERE id != ?
              AND ended_at < ?
              AND ended_at >= ?
              AND label IN ('朝食','昼食','夕食','間食','夜食','おやつ')
            ORDER BY ended_at DESC
            LIMIT 1""",
        (exclude_session_id, before_time, cutoff),
    ).fetchone()
    return dict(row) if row else None


async def _notify_unattributed_sessions(notified_session_ids: set[int]) -> None:
    """未確定セッション (person_id=0) に対して家族にLINE Quick Reply通知を送る。

    一度通知したセッションIDは notified_session_ids に記憶して重複通知しない。
    起動直後に古い未確定セッションを大量通知しないよう、過去30分以内のものに限定。
    直前に近い食事セッションがあれば「前と同じ食事」ボタンも追加。
    """
    from .db import get_conn
    from .notifier import send_line_with_quick_reply

    cutoff = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, started_at, ended_at, label, event_count
                 FROM meal_sessions
                WHERE person_id = 0 AND started_at >= ?
                ORDER BY started_at""",
            (cutoff,),
        ).fetchall()

        for r in rows:
            sid = r["id"]
            if sid in notified_session_ids:
                continue
            started = r["started_at"]
            if isinstance(started, str):
                try:
                    started_dt = datetime.fromisoformat(started)
                except ValueError:
                    started_dt = datetime.now()
            else:
                started_dt = started
            t_str = started_dt.strftime("%H:%M")

            # 直前の食事セッションを探す
            prev = _find_previous_meal_session(conn, started_dt, sid)
            prev_info = ""
            if prev:
                prev_started = prev["started_at"]
                if isinstance(prev_started, str):
                    try:
                        prev_dt = datetime.fromisoformat(prev_started)
                        prev_t = prev_dt.strftime("%H:%M")
                    except ValueError:
                        prev_t = str(prev_started)[:16]
                else:
                    prev_t = prev_started.strftime("%H:%M")
                prev_info = f"\n\n（直前 {prev_t} の {prev['label']} あり）"

            label = r["label"] or "活動"
            msg = (
                f"❓ {t_str} に「{label}」を検知しました\n"
                f"（センサー反応 {r['event_count']}件）"
                f"{prev_info}\n\n"
                "誰の行動ですか？下のボタンから選んでください。"
            )
            items = [
                {"label": "祖母", "data": f"attribute:{sid}:1"},
                {"label": "母", "data": f"attribute:{sid}:2"},
                {"label": "祖父", "data": f"attribute:{sid}:3"},
                {"label": "不明", "data": f"attribute:{sid}:0"},
            ]
            if prev:
                items.append({"label": "前と同じ食事", "data": f"merge:{sid}:{prev['id']}"})
            try:
                sent = await asyncio.to_thread(send_line_with_quick_reply, msg, items)
                if sent:
                    notified_session_ids.add(sid)
                    log.info("未確定セッション#%d の人物確認をLINE通知", sid)
            except Exception as e:
                log.warning("未確定セッション通知失敗 #%d: %s", sid, e)
    finally:
        conn.close()


async def session_aggregator(interval: int = 60) -> None:
    prev_session_count: int | None = None
    notified_session_ids: set[int] = set()

    while True:
        await asyncio.sleep(interval)
        try:
            created = aggregate_sessions()
            if not created:
                continue
            log.info("セッション集約: %d 件作成", created)

            # 未確定セッション(person_id=0)を検出して家族にLINE Quick Reply通知
            await _notify_unattributed_sessions(notified_session_ids)

            # 祖母の今日のセッション数を確認
            grandma_sessions = sessions_today(GRANDMA_ID)
            current_count = len(grandma_sessions)

            # 前回チェック時からセッションが増えた場合のみ処理
            if prev_session_count is not None and current_count > prev_session_count:
                new_count = current_count - prev_session_count
                log.info("祖母の新規食事セッション: %d件 (本日計%d件)", new_count, current_count)

                last = grandma_sessions[-1]
                last_time = last["started_at"]
                if hasattr(last_time, "strftime"):
                    last_time_str = last_time.strftime("%H:%M")
                else:
                    last_time_str = str(last_time)

                # Layer 3: 食事回数が多い場合、家族にLINE通知
                if current_count >= MAX_MEALS_BEFORE_ALERT:
                    log.warning("祖母の食事回数が%d回に到達 → LINE通知", current_count)
                    await asyncio.to_thread(notify_meal_alert, "祖母", current_count, last_time_str)

                # Layer 2: 炊飯器を自動ロック（次の使用を防止）
                if current_count >= 2:
                    warning = should_warn_recent_meal(GRANDMA_ID)
                    if warning:
                        log.info("直近食事あり(%s分前) → 炊飯器ロック", warning["minutes_ago"])
                        locked = await lock_device("rice_cooker", RICE_COOKER_NODE_ID,
                                                   reason=f"本日{current_count}回目の食事検知")
                        if locked:
                            await asyncio.to_thread(notify_device_locked, "rice_cooker")

            prev_session_count = current_count

        except Exception as e:
            log.warning("セッション集約/通知エラー: %s", e)


# --- メイン ---

async def main() -> None:
    from src.db import init_db
    init_db()

    env = _load_env()
    tapo_user = env.get("TAPO_USERNAME", "")
    tapo_pass = env.get("TAPO_PASSWORD", "")
    camera_user = env.get("CAMERA_USERNAME", "")
    camera_pass = env.get("CAMERA_PASSWORD", "")

    tasks: list[asyncio.Task] = []

    # お風呂監視 (BathMonitor)
    global _bath_monitor

    async def _on_bath_start():
        await event_bus.record_event(source="bath_door", event_type="bath_start", person_id=GRANDMA_ID)

    async def _on_bath_end(duration_min: float):
        await event_bus.record_event(
            source="bath_door", event_type="bath_end",
            person_id=GRANDMA_ID, value=duration_min,
        )

    async def _on_bath_alert(elapsed_min: float):
        msg = (
            f"🚨 緊急: 浴室で{int(elapsed_min)}分間動きがありません!\n"
            f"すぐに確認してください。"
        )
        await asyncio.to_thread(send_line_message, msg)

    _bath_monitor = BathMonitor(
        alert_minutes=30,
        on_bath_start=_on_bath_start,
        on_bath_end=_on_bath_end,
        on_alert=_on_bath_alert,
    )
    log.info("お風呂監視を初期化 (アラート: 30分)")

    # P110M 電力監視 (Matter経由)
    plug = MatterPlugMonitor(
        cfg=MatterPlugConfig(
            name="rice_cooker",
            node_id=1,
            threshold_w=float(env.get("RICE_COOKER_THRESHOLD_W", "600")),
            poll_interval=float(env.get("POLL_INTERVAL", "5")),
            idle_confirm_seconds=float(env.get("RICE_COOKER_IDLE_CONFIRM", "600")),
        ),
        on_start=on_plug_start,
        on_stop=on_plug_stop,
        on_reading=on_plug_reading,
    )
    tasks.append(asyncio.create_task(plug.run()))
    log.info("P110M電力監視を開始")

    # T110 開閉センサー (H100ハブ経由)
    hub_ip = env.get("HUB_IP", "")
    if tapo_user and tapo_pass:
        contact = ContactSensorMonitor(
            cfg=ContactSensorConfig(
                hub_ip=hub_ip,
                username=tapo_user,
                password=tapo_pass,
                poll_interval=float(env.get("POLL_INTERVAL", "5")),
            ),
            on_change=on_contact_change,
        )
        tasks.append(asyncio.create_task(contact.run()))
        log.info("T110開閉センサー監視を開始")

    # C220 カメラ
    camera_ip = env.get("CAMERA_IP", "")
    if camera_ip and camera_user:
        cam = CameraMonitor(
            cfg=CameraConfig(
                ip=camera_ip,
                username=camera_user,
                password=camera_pass,
                poll_interval=2.0,
                save_dir=Path(__file__).resolve().parent.parent / "data" / "captures",
            ),
            on_person=on_person_detected,
        )
        tasks.append(asyncio.create_task(cam.run()))
        log.info("C220カメラ監視を開始")

    # セッション集約
    tasks.append(asyncio.create_task(session_aggregator(60)))

    log.info("=== 全センサー統合監視 稼働中 (%d タスク) ===", len(tasks))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            log.error("タスク %d でエラー: %s", i, r)


if __name__ == "__main__":
    asyncio.run(main())
