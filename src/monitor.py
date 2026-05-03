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


RICE_COOKING_CERTAIN_W = 700  # これ以上で確実に炊飯と判定（ambiguous問い合わせをスキップ）


async def on_plug_start(name: str, r: PlugReading) -> None:
    import time
    # 炊飯器: 蓋開直後の power_on は保温ヒーター応答として抑制
    if name == "rice_cooker":
        last_lid_open = _recent_lid_opens.get("rice_cooker_lid", 0.0)
        elapsed = time.time() - last_lid_open
        if 0 < elapsed <= LID_OPEN_SUPPRESS_SECONDS:
            log.info(
                "[%s] 蓋開%.0f秒後の power_on(%.0fW) → 保温応答として抑制",
                name, elapsed, r.power_w,
            )
            return

    event_id = await event_bus.record_event(
        source=name,
        event_type="power_on",
        value=r.power_w,
    )

    # 炊飯器の中間電力（100〜700W）→ 炊飯/保温/蓋開のいずれか曖昧
    # 家族に LINE Quick Reply で問い合わせる
    if name == "rice_cooker" and r.power_w < RICE_COOKING_CERTAIN_W:
        await _ask_rice_action_classification(event_id, r.power_w)

    # ドライヤー稼働開始 → 直近30分以内にお風呂が終わっていれば「髪洗った」と推定
    if name == "hair_dryer":
        await _maybe_record_hair_wash()


async def _ask_rice_action_classification(event_id: int, power_w: float) -> None:
    """炊飯器の曖昧な電力検知時、家族にLINE Quick Replyで分類を仰ぐ。"""
    from src.notifier import broadcast_with_quick_reply, record_pending_notification
    now_str = datetime.now().strftime("%H:%M")
    msg = (
        f"🍚 炊飯器が起動しました\n"
        f"電力: {int(power_w)}W / 時刻: {now_str}\n\n"
        "これは何の動きですか？"
    )
    items = [
        {"label": "炊飯", "data": f"rice_action:{event_id}:cook"},
        {"label": "保温", "data": f"rice_action:{event_id}:keep_warm"},
        {"label": "蓋開のみ", "data": f"rice_action:{event_id}:lid_only"},
        {"label": "不明", "data": f"rice_action:{event_id}:unknown"},
    ]
    try:
        sent = await asyncio.to_thread(broadcast_with_quick_reply, msg, items)
        if sent > 0:
            await asyncio.to_thread(
                record_pending_notification,
                "rice_action", f"event_{event_id}", msg, items,
            )
            log.info("[rice_cooker] 曖昧電力%.0fW → 家族に分類問い合わせ送信(event=%d)",
                     power_w, event_id)
    except Exception as e:
        log.warning("rice_action 通知失敗: %s", e)


async def on_plug_stop(name: str, r: PlugReading) -> None:
    await event_bus.record_event(
        source=name,
        event_type="power_off",
        value=r.power_w,
    )


HAIR_WASH_AFTER_BATH_MINUTES = 30


async def _maybe_record_hair_wash() -> None:
    """ドライヤー稼働開始時に呼ばれる。

    判定根拠（多層）:
    - 直近30分以内に bath_end あり → 「入浴後ドライヤー使用」（信頼度0.9）
    - 直近30分以内に shower_start もあれば信頼度↑（0.95）
    - 入浴記録なくドライヤー単独 → 髪洗いとは判定しない

    1日1回までに制限。
    """
    from datetime import datetime, timedelta
    from src.db import get_conn
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        already = conn.execute(
            """SELECT 1 FROM events
                WHERE source = 'hair_dryer' AND event_type = 'hair_wash'
                  AND started_at >= ?""",
            (today + " 00:00:00",),
        ).fetchone()
        if already:
            log.info("[hair_dryer] 本日既に髪洗い記録あり → 重複記録スキップ")
            return
        cutoff = (datetime.now() - timedelta(minutes=HAIR_WASH_AFTER_BATH_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        bath_end = conn.execute(
            """SELECT started_at FROM events
                WHERE source = 'bath_door' AND event_type = 'bath_end'
                  AND started_at >= ?
                ORDER BY started_at DESC LIMIT 1""",
            (cutoff,),
        ).fetchone()
        # SwitchBot温湿度計の shower_start もあれば信頼度UP
        shower = conn.execute(
            """SELECT 1 FROM events
                WHERE source = 'bathroom_meter' AND event_type = 'shower_start'
                  AND started_at >= ?""",
            (cutoff,),
        ).fetchone()
    finally:
        conn.close()

    if not bath_end:
        log.info("[hair_dryer] 直近30分の入浴終了なし → 髪洗い判定保留")
        return

    confidence = 0.95 if shower else 0.9
    extra = "（湿度急上昇も検知）" if shower else ""
    await event_bus.record_event(
        source="hair_dryer",
        event_type="hair_wash",
        person_id=GRANDMA_ID,
        confidence=confidence,
    )
    log.info("[hair_dryer] 髪洗い検知（信頼度=%.2f）%s", confidence, extra)
    try:
        from src.notifier import send_line_message
        await asyncio.to_thread(
            send_line_message,
            f"💇 祖母が髪を洗ったようです\n（入浴後にドライヤー使用を検知）{extra}",
        )
    except Exception as e:
        log.warning("髪洗い通知失敗: %s", e)


_bath_monitor: BathMonitor | None = None

# T110のエイリアスで設置場所を判定（Tapoアプリでリネームする想定）
BATH_DOOR_ALIASES = {"浴室ドア", "bath_door", "風呂ドア"}
BATH_MOTION_ALIASES = {"脱衣所", "bath_motion", "脱衣所モーション"}
RICE_COOKER_LID_ALIASES = {"炊飯器", "炊飯器の蓋", "rice_cooker_lid", "Rice Cooker"}
FRIDGE_ALIASES = {"冷蔵庫", "fridge", "Refrigerator"}
TOILET_DOOR_ALIASES = {"トイレ", "toilet", "toilet_door", "Toilet"}
TOOTHBRUSH_ALIASES = {"歯ブラシ", "歯ブラシスタンド", "toothbrush", "Toothbrush"}
SHAMPOO_ALIASES = {"シャンプー", "シャンプーボトル", "shampoo", "shampoo_bottle"}

# 蓋開→保温応答 power_on の抑制窓（秒）
LID_OPEN_SUPPRESS_SECONDS = 30
_recent_lid_opens: dict[str, float] = {}  # device_name -> last_open_unix_time


def _alias_to_source(alias: str) -> str:
    """T110/T100のエイリアスから内部ソース名へ変換。"""
    if alias in BATH_DOOR_ALIASES:
        return "bath_door"
    if alias in BATH_MOTION_ALIASES:
        return "bath_motion"
    if alias in RICE_COOKER_LID_ALIASES:
        return "rice_cooker_lid"
    if alias in FRIDGE_ALIASES:
        return "fridge"
    if alias in TOILET_DOOR_ALIASES:
        return "toilet_door"
    if alias in TOOTHBRUSH_ALIASES:
        return "toothbrush"
    if alias in SHAMPOO_ALIASES:
        return "shampoo_bottle"
    if alias == "Tapo T110":
        return "contact_sensor"
    return alias


async def on_contact_change(event: ContactEvent) -> None:
    import time
    alias = event.alias
    # 浴室ドアセンサーの場合 → BathMonitor に委譲
    if alias in BATH_DOOR_ALIASES and _bath_monitor:
        if event.is_open:
            await _bath_monitor.door_opened()
        else:
            await _bath_monitor.door_closed()

    source = _alias_to_source(alias)

    # 炊飯器の蓋開検知 → 保温応答 power_on の抑制窓を有効化
    if source == "rice_cooker_lid" and event.is_open:
        _recent_lid_opens["rice_cooker_lid"] = time.time()

    await event_bus.record_event(
        source=source,
        event_type="open" if event.is_open else "close",
    )


async def on_motion_detected(event: ContactEvent) -> None:
    """T100モーションセンサー: 脱衣所など各設置場所の動き検知。"""
    alias = event.alias
    if alias in BATH_MOTION_ALIASES and _bath_monitor:
        await _bath_monitor.motion_detected()
    source = _alias_to_source(alias) if alias in BATH_MOTION_ALIASES else alias
    await event_bus.record_event(
        source=source,
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


async def _dryer_reminder_loop():
    """入浴終了後30分経ってもドライヤー使用が無い場合、家族に確認通知。

    1日1回のみ通知（重複防止）。
    """
    from datetime import datetime, timedelta
    from src.db import get_conn
    from src.notifier import send_actionable_notification

    notified_today: set[str] = set()  # date string
    while True:
        await asyncio.sleep(15 * 60)  # 15分おき
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if today in notified_today:
                continue

            conn = get_conn()
            try:
                # 直近の入浴終了
                bath_end = conn.execute(
                    """SELECT started_at FROM events
                        WHERE source = 'bath_door' AND event_type = 'bath_end'
                          AND started_at >= ?
                        ORDER BY started_at DESC LIMIT 1""",
                    (today + " 00:00:00",),
                ).fetchone()
                # 今日の髪洗い記録
                hair_wash = conn.execute(
                    """SELECT 1 FROM events
                        WHERE source = 'hair_dryer' AND event_type = 'hair_wash'
                          AND started_at >= ?""",
                    (today + " 00:00:00",),
                ).fetchone()
            finally:
                conn.close()

            if not bath_end or hair_wash:
                continue

            be_time = bath_end["started_at"]
            if isinstance(be_time, str):
                try:
                    be_time = datetime.fromisoformat(be_time)
                except ValueError:
                    continue

            elapsed = (datetime.now() - be_time).total_seconds() / 60
            if elapsed < HAIR_WASH_AFTER_BATH_MINUTES:
                continue  # まだ猶予内

            await asyncio.to_thread(
                send_actionable_notification,
                "hair_dryer_missing", today,
                f"💇 祖母がお風呂後にドライヤーを使った形跡がありません\n"
                f"入浴終了: {be_time.strftime('%H:%M')} から {int(elapsed)}分経過\n\n"
                "髪を洗ったか、ドライヤーで乾かすよう声かけしてください。",
            )
            notified_today.add(today)
            log.info("ドライヤー未使用の確認通知を送信")
        except Exception as e:
            log.warning("ドライヤー確認ループエラー: %s", e)


async def _notify_unattributed_sessions(notified_session_ids: set[int]) -> None:
    """未確定セッション (person_id=0) に対して家族にLINE Quick Reply通知を送る。

    一度通知したセッションIDは notified_session_ids に記憶して重複通知しない。
    起動直後に古い未確定セッションを大量通知しないよう、過去30分以内のものに限定。
    直前に近い食事セッションがあれば「前と同じ食事」ボタンも追加。
    """
    from .db import get_conn
    from .notifier import broadcast_with_quick_reply, record_pending_notification

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
            # 登録済み人物（id>0）を Quick Reply ボタンに展開
            person_rows = conn.execute(
                "SELECT id, name FROM persons WHERE id > 0 ORDER BY id"
            ).fetchall()
            items = [
                {"label": p["name"], "data": f"attribute:{sid}:{p['id']}"}
                for p in person_rows
            ]
            items.append({"label": "不明", "data": f"attribute:{sid}:0"})
            if prev:
                items.append({"label": "前と同じ食事", "data": f"merge:{sid}:{prev['id']}"})
            # LINEのQuick Replyは最大13個
            items = items[:13]
            try:
                sent = await asyncio.to_thread(broadcast_with_quick_reply, msg, items)
                if sent:
                    notified_session_ids.add(sid)
                    # 再通知用に pending_notifications にも記録
                    await asyncio.to_thread(
                        record_pending_notification,
                        "attribute_session", f"session_{sid}", msg, items
                    )
                    log.info("未確定セッション#%d の人物確認をブロードキャスト (送信先 %d)", sid, sent)
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
        from .notifier import send_actionable_notification
        msg = (
            f"🚨 緊急: 浴室で{int(elapsed_min)}分間動きがありません!\n"
            f"すぐに確認してください。"
        )
        ctx = datetime.now().strftime("%Y-%m-%d_%H%M")
        await asyncio.to_thread(
            send_actionable_notification, "bath_emergency", ctx, msg
        )

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

    # SwitchBot 温湿度計（浴室）監視（SWITCHBOT_METER_ENABLED=1 のときのみ稼働）
    sb_enabled = env.get("SWITCHBOT_METER_ENABLED", "0") == "1"
    sb_mac = env.get("SWITCHBOT_METER_MAC", "").strip()
    if sb_enabled and sb_mac:
        from src.sensors.switchbot_meter import SwitchBotMeterMonitor, MeterReading
        from src.bath_humidity_detector import BathHumidityDetector
        _humidity_detector = BathHumidityDetector()

        async def _on_meter_reading(r: "MeterReading") -> None:
            await event_bus.record_event(
                source="bathroom_meter",
                event_type="reading",
                value=float(r.humidity_pct),
            )
            events_out = _humidity_detector.feed(r.humidity_pct, r.temperature_c, r.timestamp)
            for ev_type, payload in events_out:
                await event_bus.record_event(
                    source="bathroom_meter",
                    event_type=ev_type,
                    person_id=GRANDMA_ID if ev_type in ("shower_start", "shower_end") else None,
                    value=float(r.humidity_pct),
                )
                if ev_type == "abnormal_temp":
                    from src.notifier import send_actionable_notification
                    msg = (
                        f"🚨 浴室の温度が異常です\n"
                        f"室温: {payload['temperature']:.1f}℃ / 湿度: {payload['humidity']}%\n"
                        "ヒートショックの危険があります。確認してください。"
                    )
                    ctx = datetime.now().strftime("%Y-%m-%d_%H%M")
                    await asyncio.to_thread(
                        send_actionable_notification, "bath_abnormal_temp", ctx, msg
                    )

        meter = SwitchBotMeterMonitor(
            target_mac=sb_mac,
            poll_seconds=float(env.get("SWITCHBOT_METER_POLL_SECONDS", "10")),
            on_reading=_on_meter_reading,
        )
        tasks.append(asyncio.create_task(meter.run()))
        log.info("SwitchBot 温湿度計 BLE 監視を開始（MAC=%s）", sb_mac)
    else:
        log.info(
            "SwitchBot 温湿度計監視は無効（SWITCHBOT_METER_ENABLED=%s, MAC=%s）",
            env.get("SWITCHBOT_METER_ENABLED", "0"),
            "未設定" if not sb_mac else "設定済み",
        )

    # ドライヤー監視（HAIR_DRYER_NODE_ID > 0 のときのみ稼働）
    hair_dryer_node = int(env.get("HAIR_DRYER_NODE_ID", "0"))
    if hair_dryer_node > 0:
        dryer = MatterPlugMonitor(
            cfg=MatterPlugConfig(
                name="hair_dryer",
                node_id=hair_dryer_node,
                threshold_w=float(env.get("HAIR_DRYER_THRESHOLD_W", "300")),
                poll_interval=float(env.get("POLL_INTERVAL", "5")),
                idle_confirm_seconds=float(env.get("HAIR_DRYER_IDLE_CONFIRM", "30")),
            ),
            on_start=on_plug_start,
            on_stop=on_plug_stop,
        )
        tasks.append(asyncio.create_task(dryer.run()))
        log.info("ドライヤー P110M監視を開始 (node=%d)", hair_dryer_node)
    else:
        log.info("HAIR_DRYER_NODE_ID 未設定 → ドライヤー監視は無効")

    # 入浴後ドライヤー未使用の促し（15分おきにチェック）
    tasks.append(asyncio.create_task(_dryer_reminder_loop()))

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
