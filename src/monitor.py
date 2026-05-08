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

# 学習に基づく自動分類のパラメータ
RICE_AUTO_POWER_WINDOW = 50      # 過去サンプルとの power_w 許容差
RICE_AUTO_HOUR_WINDOW = 2        # 過去サンプルとの hour_of_day 許容差
RICE_AUTO_MIN_SAMPLES = 3        # この件数以上の類似サンプルがあれば自動判定可
RICE_AUTO_AGREEMENT = 0.8        # 80%以上同じ分類なら採用


def _predict_rice_action(power_w: float, hour: int, lid_recent: bool) -> tuple[str, float] | None:
    """過去の家族分類から、新しいイベントの分類を予測する。

    戻り値: (分類, 信頼度) または None（データ不足/曖昧）。
    'unknown' 分類は学習対象から除外。
    """
    from src.db import get_conn
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT classification, COUNT(*) as cnt
                 FROM rice_classifications
                WHERE ABS(power_w - ?) <= ?
                  AND ABS(hour_of_day - ?) <= ?
                  AND lid_recently_opened = ?
                  AND classification != 'unknown'
                  AND auto_decided = 0
                GROUP BY classification""",
            (power_w, RICE_AUTO_POWER_WINDOW,
             hour, RICE_AUTO_HOUR_WINDOW,
             1 if lid_recent else 0),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None
    total = sum(r["cnt"] for r in rows)
    if total < RICE_AUTO_MIN_SAMPLES:
        return None
    rows_sorted = sorted(rows, key=lambda r: -r["cnt"])
    top = rows_sorted[0]
    confidence = top["cnt"] / total
    if confidence < RICE_AUTO_AGREEMENT:
        return None
    return (top["classification"], confidence)


def _record_classification(event_id: int | None, power_w: float, hour: int,
                            lid_recent: bool, classification: str,
                            classified_by: str | None, auto_decided: bool) -> None:
    """家族の分類 or 自動分類を rice_classifications に記録。"""
    from src.db import transaction
    try:
        with transaction() as c:
            c.execute(
                """INSERT INTO rice_classifications
                       (event_id, power_w, hour_of_day, lid_recently_opened,
                        classification, classified_by, auto_decided)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (event_id, power_w, hour, 1 if lid_recent else 0,
                 classification, classified_by, 1 if auto_decided else 0),
            )
    except Exception as e:
        log.warning("rice_classifications 記録失敗: %s", e)


async def on_plug_start(name: str, r: PlugReading) -> None:
    import time
    # 炊飯器: 蓋開直後の power_on は保温ヒーター応答として抑制
    lid_recent = False
    if name == "rice_cooker":
        last_lid_open = _recent_lid_opens.get("rice_cooker_lid", 0.0)
        elapsed = time.time() - last_lid_open
        lid_recent = 0 < elapsed <= LID_OPEN_SUPPRESS_SECONDS
        if lid_recent:
            log.info(
                "[%s] 蓋開%.0f秒後の power_on(%.0fW) → 保温応答として抑制",
                name, elapsed, r.power_w,
            )
            return

    # 炊飯器: 高電力なら確実に炊飯、それ以外は学習データ + 蓋センサ状態を組み合わせて推定
    if name == "rice_cooker" and r.power_w < RICE_COOKING_CERTAIN_W:
        hour = datetime.now().hour
        prediction = _predict_rice_action(r.power_w, hour, lid_recent)

        # 蓋が開いていないかつ、保温の学習データが1件でもあれば「保温」と推定して通知抑制
        # 蓋を開けずに電力が動く = 炊飯器内部のヒーターサイクル → 保温で確定的
        if prediction is None and not lid_recent:
            from src.db import get_conn
            try:
                conn = get_conn()
                kw_count = conn.execute(
                    """SELECT COUNT(*) FROM rice_classifications
                        WHERE classification = 'keep_warm'
                          AND auto_decided = 0
                          AND ABS(power_w - ?) <= ?""",
                    (r.power_w, RICE_AUTO_POWER_WINDOW),
                ).fetchone()[0]
                conn.close()
                if kw_count >= 1:
                    prediction = ("keep_warm", 0.7)
                    log.info(
                        "[rice_cooker] 蓋閉+保温学習%d件 → 保温と推定（電力%.0fW）",
                        kw_count, r.power_w,
                    )
            except Exception as e:
                log.warning("保温学習チェック失敗: %s", e)

        if prediction:
            cls, conf = prediction
            log.info(
                "[rice_cooker] 自動分類: %s (信頼度%.0f%%, 電力%.0fW, 時刻%d時)",
                cls, conf * 100, r.power_w, hour,
            )
            if cls in ("keep_warm", "lid_only"):
                # 食事ではないのでイベント記録しない（食事カウントに影響させない）
                _record_classification(
                    None, r.power_w, hour, lid_recent, cls,
                    classified_by="auto", auto_decided=True,
                )
                return
            # 'cook' なら通常通り記録（直近カメラ識別人物を帰属候補に）
            event_id = await event_bus.record_event(
                source=name, event_type="power_on", value=r.power_w,
                person_id=get_active_person(),
            )
            _record_classification(
                event_id, r.power_w, hour, lid_recent, cls,
                classified_by="auto", auto_decided=True,
            )
            return

    event_id = await event_bus.record_event(
        source=name,
        event_type="power_on",
        value=r.power_w,
        person_id=get_active_person(),
    )

    # 炊飯器の中間電力で予測できなかった → 家族に問い合わせ
    if name == "rice_cooker" and r.power_w < RICE_COOKING_CERTAIN_W:
        await _ask_rice_action_classification(event_id, r.power_w)

    # ドライヤー稼働開始 → 直近30分以内にお風呂が終わっていれば「髪洗った」と推定
    if name == "hair_dryer":
        await _maybe_record_hair_wash()


async def _request_long_toilet_alert(duration_sec: float) -> None:
    """トイレに長時間滞在 → 家族に確認LINE。

    認知症の祖母が転倒等で動けなくなっている可能性があるため、家族に状況確認を促す。
    """
    from datetime import datetime as _dt
    from src.notifier import send_actionable_notification
    minutes = int(duration_sec / 60)
    ctx = _dt.now().strftime("%Y-%m-%d_%H%M_long_toilet")
    msg = (
        f"⚠️ トイレに {minutes} 分以上滞在しています\n"
        f"時刻: {_dt.now().strftime('%H:%M')}\n\n"
        "祖母さんが転倒等で動けなくなっていないか、確認してあげてください。"
    )
    try:
        await asyncio.to_thread(
            send_actionable_notification, "long_toilet_stay", ctx, msg
        )
        log.warning("[toilet] 長時間滞在アラート: %d分", minutes)
    except Exception as e:
        log.warning("長時間トイレアラート送信失敗: %s", e)


async def _request_session_confirmation(session_id: int) -> None:
    """新規未確定セッションについて家族にLINEで確認を送信。

    家族の選択:
      - 祖母が食事した
      - 祖父が食事した
      - 母/他の家族が食事した
      - 食事ではない（誤検知）
    回答に応じて confirmed=1 / -1 にマークし person_id を確定する。
    """
    from src.notifier import broadcast_with_quick_reply, record_pending_notification
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, started_at, label, event_count FROM meal_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        # このセッションに含まれるソース別の反応回数を取得（判定根拠）
        source_rows = conn.execute(
            """SELECT e.source, COUNT(*) as cnt
                 FROM session_events se
                 JOIN events e ON e.id = se.event_id
                WHERE se.session_id = ?
                GROUP BY e.source
                ORDER BY cnt DESC""",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    if not row:
        return
    label = row["label"] or "セッション"
    started = row["started_at"]
    if isinstance(started, str) and " " in started:
        time_str = started.split(" ")[1][:5]
    else:
        time_str = str(started)

    # お風呂セッションは bath_classification 系で別途確認するためスキップ
    if label == "お風呂":
        return

    # 判定根拠を組み立て: どのセンサーが何回反応したか
    source_labels = {
        "rice_cooker": "炊飯器電源",
        "rice_cooker_lid": "炊飯器の蓋",
        "fridge": "冷蔵庫",
        "toilet_door": "トイレ",
        "bath_door": "風呂",
        "bath_motion": "脱衣所",
        "camera": "カメラ",
    }
    basis_parts = [f"{source_labels.get(s['source'], s['source'])}×{s['cnt']}" for s in source_rows]
    basis_str = "、".join(basis_parts) if basis_parts else "（センサー詳細不明）"

    msg = (
        f"🍱 {time_str}頃 「{label}」の動きをセンサーが検知しました\n"
        f"判定根拠: {basis_str}\n\n"
        "誰の食事ですか？"
    )
    items = [
        {"label": "祖母さん", "data": f"sess_confirm:{session_id}:1"},
        {"label": "祖父さん", "data": f"sess_confirm:{session_id}:3"},
        {"label": "母さん", "data": f"sess_confirm:{session_id}:2"},
        {"label": "他の家族", "data": f"sess_confirm:{session_id}:other"},
        {"label": "食事じゃない", "data": f"sess_confirm:{session_id}:reject"},
    ]
    try:
        sent = await asyncio.to_thread(broadcast_with_quick_reply, msg, items)
        if sent > 0:
            await asyncio.to_thread(
                record_pending_notification,
                "session_confirm", f"session_{session_id}", msg, items,
            )
            log.info("[session_confirm] 確認送信 sid=%d", session_id)
    except Exception as e:
        log.warning("セッション確認LINE送信失敗: %s", e)


async def _request_lock_confirmation(meal_count: int, minutes_ago: int) -> None:
    """炊飯器ロック実行前に家族にLINEで確認を取る（誤検知防止）。

    家族の [はい] 押下で実際にロック実行、[いいえ] でスキップ。
    """
    from src.notifier import broadcast_with_quick_reply, record_pending_notification
    from datetime import datetime as _dt
    now = _dt.now()
    ctx = now.strftime("%Y-%m-%d_%H%M_lockreq")
    msg = (
        f"🍚 祖母さんが本日{meal_count}回目の食事を検知しました\n"
        f"前回の食事から {minutes_ago} 分経過\n\n"
        "炊飯器をロックしますか？"
    )
    items = [
        {"label": "ロックする", "data": f"lock_confirm:{ctx}:yes"},
        {"label": "ロックしない", "data": f"lock_confirm:{ctx}:no"},
    ]
    try:
        sent = await asyncio.to_thread(broadcast_with_quick_reply, msg, items)
        if sent > 0:
            await asyncio.to_thread(
                record_pending_notification,
                "lock_confirm", ctx, msg, items,
            )
            log.info("[lock_confirm] 確認送信 ctx=%s", ctx)
    except Exception as e:
        log.warning("ロック確認LINE送信失敗: %s", e)


async def _ask_rice_action_classification(event_id: int, power_w: float) -> None:
    """炊飯器の曖昧な電力検知時、家族にLINE Quick Replyで分類を仰ぐ。

    蓋センサーが未稼働なら問い合わせも発火しない（蓋情報なしでは曖昧解消が
    家族側でも難しいため、LINEノイズを避ける）。蓋センサーが稼働開始したら
    自動的に問い合わせモードに入る。
    """
    from src.db import get_conn
    from src.notifier import broadcast_with_quick_reply, record_pending_notification
    # 蓋センサー稼働確認 + 直近の蓋開検知必須化
    # 「蓋が開いていない」 = 保温/ヒーター応答 = 食事の可能性ゼロなので問い合わせも発火させない
    # （これまで保温パルスのたびに問い合わせが来ていた問題の根本治癒）
    conn = get_conn()
    try:
        lid_active = conn.execute(
            "SELECT 1 FROM events WHERE source='rice_cooker_lid' AND started_at >= datetime('now','-24 hours') LIMIT 1"
        ).fetchone() is not None
        # 直近10分以内に蓋が開いた形跡があるか
        lid_recent_open = conn.execute(
            """SELECT 1 FROM events
                WHERE source='rice_cooker_lid' AND event_type='open'
                  AND started_at >= datetime('now', '-10 minutes')
                LIMIT 1"""
        ).fetchone() is not None
    finally:
        conn.close()
    if not lid_active:
        log.info("[rice_cooker] 蓋センサー未稼働 → 分類問い合わせを抑制")
        return
    if not lid_recent_open:
        log.info(
            "[rice_cooker] 直近10分に蓋開なし(%.0fW) → 保温/ヒーター応答とみなし問い合わせ抑制",
            power_w,
        )
        return

    now_str = datetime.now().strftime("%H:%M")
    msg = (
        f"🍚 炊飯器が起動しました\n"
        f"電力: {int(power_w)}W / 時刻: {now_str}\n\n"
        "これは何の動きですか？"
    )
    items = [
        {"label": "炊飯", "data": f"rice_action:{event_id}:cook"},
        {"label": "保温", "data": f"rice_action:{event_id}:keep_warm"},
        {"label": "蓋を開けただけ", "data": f"rice_action:{event_id}:lid_only"},
        {"label": "開けてご飯食べた", "data": f"rice_action:{event_id}:lid_meal"},
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
_bath_detector = None  # BathDetector instance（後で初期化）

# T110のエイリアスで設置場所を判定（Tapoアプリでリネームする想定）
BATH_DOOR_ALIASES = {"浴室ドア", "bath_door", "風呂ドア", "風呂"}
BATH_MOTION_ALIASES = {"脱衣所", "bath_motion", "脱衣所モーション"}
RICE_COOKER_LID_ALIASES = {"炊飯器", "炊飯器の蓋", "rice_cooker_lid", "Rice Cooker"}
FRIDGE_ALIASES = {"冷蔵庫", "fridge", "Refrigerator"}
TOILET_DOOR_ALIASES = {"トイレ", "toilet", "toilet_door", "Toilet"}
TOOTHBRUSH_ALIASES = {"歯ブラシ", "歯ブラシスタンド", "toothbrush", "Toothbrush"}
SHAMPOO_ALIASES = {"シャンプー", "シャンプーボトル", "shampoo", "shampoo_bottle"}

# 蓋開→保温応答 power_on の抑制窓（秒）
LID_OPEN_SUPPRESS_SECONDS = 30
_recent_lid_opens: dict[str, float] = {}  # device_name -> last_open_unix_time

# トイレ滞在時間の判定（秒）
TOILET_SHORT_PASS_SECONDS = 10      # 10秒未満は「通り過ぎ」扱い（サマリでは表示するが、アラートは出さない）
TOILET_LONG_STAY_SECONDS = 5 * 60   # 5分以上で長時間滞在アラート
_recent_toilet_opens: dict[str, float] = {}  # 'last' -> open unix time


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
    from datetime import datetime as _dt
    alias = event.alias
    # 浴室ドアセンサーの場合 → BathMonitor + BathDetector に通知
    if alias in BATH_DOOR_ALIASES and _bath_monitor:
        if event.is_open:
            await _bath_monitor.door_opened()
        else:
            await _bath_monitor.door_closed()
    if alias in BATH_DOOR_ALIASES and _bath_detector is not None:
        _bath_detector.feed_door(event.is_open, _dt.now())

    source = _alias_to_source(alias)

    # 炊飯器の蓋開検知 → 保温応答 power_on の抑制窓を有効化
    if source == "rice_cooker_lid" and event.is_open:
        _recent_lid_opens["rice_cooker_lid"] = time.time()

    # トイレドアの open 時刻を保持（close 時に滞在時間を計算）
    if source == "toilet_door" and event.is_open:
        _recent_toilet_opens["last"] = time.time()

    # 直近のカメラ識別人物を帰属候補に
    inferred_pid = get_active_person()
    await event_bus.record_event(
        source=source,
        event_type="open" if event.is_open else "close",
        person_id=inferred_pid,
    )

    # トイレ close 時: 滞在時間を計算して長時間ならアラート
    if source == "toilet_door" and not event.is_open:
        last_open = _recent_toilet_opens.get("last", 0.0)
        if last_open:
            duration_sec = time.time() - last_open
            if duration_sec >= TOILET_LONG_STAY_SECONDS:
                await _request_long_toilet_alert(duration_sec)


async def on_motion_detected(event: ContactEvent) -> None:
    """T100モーションセンサー: 脱衣所など各設置場所の動き検知。"""
    from datetime import datetime as _dt
    alias = event.alias
    if alias in BATH_MOTION_ALIASES and _bath_monitor:
        await _bath_monitor.motion_detected()
    if alias in BATH_MOTION_ALIASES and _bath_detector is not None:
        _bath_detector.feed_motion(_dt.now())
    source = _alias_to_source(alias) if alias in BATH_MOTION_ALIASES else alias
    # 直近のカメラ識別人物を帰属候補に（脱衣所/トイレ等の動きは祖母であることが多いが念のため）
    inferred_pid = get_active_person()
    await event_bus.record_event(
        source=source,
        event_type="motion",
        person_id=inferred_pid,
    )


_last_person_detection = 0.0

# 直近にカメラで識別された人物（センサー帰属ヒントとして使う）
# (timestamp_unix, person_id, name) のリスト
_camera_identification_log: list[tuple[float, int, str]] = []
ACTIVE_PERSON_WINDOW_MIN = 15   # 何分前までのカメラ識別をセンサー帰属に使うか
CAMERA_LOG_RETENTION_MIN = 60   # この分数より古いログは削除


def _record_camera_identification(person_id: int, name: str) -> None:
    """カメラで識別された人物をログに追加（古いものは削除）。"""
    import time
    now = time.time()
    _camera_identification_log.append((now, person_id, name))
    cutoff = now - CAMERA_LOG_RETENTION_MIN * 60
    while _camera_identification_log and _camera_identification_log[0][0] < cutoff:
        _camera_identification_log.pop(0)


def get_active_person() -> int | None:
    """直近にカメラで識別された人物の person_id を返す（時間相関を考慮）。

    時間重み付け方式:
      - 直近2分以内の識別 → 信頼度高
      - 2-5分前の識別 → 中
      - 5-15分前 → 低（最新一致のみ採用）
      - 15分超 → None
    複数の人物が混在する場合は出現回数も考慮した重み付けスコアで決定。
    識別ログがなければ None（→ センサー帰属できず NULL のまま）。
    """
    import time
    now = time.time()
    cutoff = now - ACTIVE_PERSON_WINDOW_MIN * 60
    recent = [r for r in _camera_identification_log if r[0] >= cutoff]
    if not recent:
        return None

    # 直近2分以内に識別されている人がいれば優先（高信頼度）
    very_recent = [r for r in recent if r[0] >= now - 120]
    if very_recent:
        # 直近2分の中で最新の識別を採用
        return very_recent[-1][1]

    # 各 person_id の重み付けスコアを計算（新しいほど重み大）
    # 重み = 1 - (経過秒数 / window_seconds) の単純な減衰
    scores: dict[int, float] = {}
    window = ACTIVE_PERSON_WINDOW_MIN * 60
    for ts, pid, _name in recent:
        weight = max(0.0, 1.0 - (now - ts) / window)
        scores[pid] = scores.get(pid, 0.0) + weight

    if not scores:
        return None
    # 最大スコアの person_id を返す
    return max(scores.items(), key=lambda x: x[1])[0]


async def on_person_detected(frame: CameraFrame) -> None:
    """カメラで人物検知時のコールバック。

    顔識別ができた場合 → person_id 付きで camera/person_detected を記録、
    active_person ログにも追加。
    顔識別できなかった場合 → person_id=None で記録（従来挙動）。
    """
    import time
    global _last_person_detection
    now = time.time()
    if now - _last_person_detection < 30:
        return
    _last_person_detection = now

    # 顔識別あり → 識別された人物ごとにイベント記録
    identified = frame.identified_persons or []
    matched_persons = [r for r in identified
                       if r.get("person_id") and r.get("confidence", 0) >= 0.6]

    if matched_persons:
        for r in matched_persons:
            pid = r["person_id"]
            await event_bus.record_event(
                source="camera",
                event_type="person_detected",
                person_id=pid,
                value=float(frame.face_count),
                confidence=float(r.get("confidence", 0)),
            )
            _record_camera_identification(pid, r.get("name", "?"))
    else:
        # 顔識別なし or 信頼度低 → person_id=None で記録（従来挙動）
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
    HAIR_DRYER_NODE_ID が未設定（=0）の場合、そもそもドライヤー使用を検知できないので
    このループは起動しない（必ず誤発火する＝オオカミ少年通知になるため）。
    """
    from datetime import datetime, timedelta
    from src.db import get_conn
    from src.notifier import send_actionable_notification

    # ドライヤー監視が無効ならスキップ
    env = _load_env()
    if env.get("HAIR_DRYER_NODE_ID", "0") == "0":
        log.info("HAIR_DRYER_NODE_ID 未設定 → ドライヤーリマインドループは起動しない")
        return

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
    confirm_asked_ids: set[int] = set()

    while True:
        await asyncio.sleep(interval)
        try:
            created_ids = aggregate_sessions()
            if not created_ids:
                continue
            log.info("セッション集約: %d 件作成（未確定）", len(created_ids))

            # 各新規セッションに対してLINEで確認を送信
            for sid in created_ids:
                if sid in confirm_asked_ids:
                    continue
                confirm_asked_ids.add(sid)
                await _request_session_confirmation(sid)

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

                # Layer 2: 炊飯器ロックは家族確認後に実行（誤検知防止）
                if current_count >= 2:
                    warning = should_warn_recent_meal(GRANDMA_ID)
                    if warning:
                        log.info(
                            "直近食事あり(%s分前) → ロック確認をLINEに送信",
                            warning["minutes_ago"],
                        )
                        await _request_lock_confirmation(current_count, warning["minutes_ago"])

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
        from src.bath_detector import BathDetector, BathCandidate
        from src.db import DB_PATH
        _humidity_detector = BathHumidityDetector()

        async def _on_bath_candidate(c: "BathCandidate", record_id: int) -> None:
            """お風呂利用候補検知時、家族にLINE Quick Replyで誰か聞く（学習データ収集）。"""
            from src.notifier import broadcast_with_quick_reply, record_pending_notification
            door_hint = "ドア閉" if c.door_was_closed else "ドア開"
            motion_hint = f"脱衣所モーション{c.motion_count}回" if c.motion_count else "モーション無"
            active = ""
            if c.active_person_id:
                conn = get_conn()
                try:
                    row = conn.execute(
                        "SELECT name FROM persons WHERE id = ?", (c.active_person_id,)
                    ).fetchone()
                    if row:
                        active = f" / カメラ識別: {row['name']}"
                finally:
                    conn.close()
            msg = (
                f"🛁 浴室で湿度上昇を検知（湿度 {c.humidity_baseline:.0f}→{c.humidity_peak:.0f}%, "
                f"温度+{c.temperature_delta:.1f}℃, {door_hint}, {motion_hint}{active}）\n\n"
                "誰がお風呂に入っていますか？"
            )
            items = [
                {"label": "祖母", "data": f"bath_cls:{record_id}:grandma"},
                {"label": "祖父", "data": f"bath_cls:{record_id}:grandpa"},
                {"label": "母", "data": f"bath_cls:{record_id}:mother"},
                {"label": "他", "data": f"bath_cls:{record_id}:other"},
                {"label": "湯はり中", "data": f"bath_cls:{record_id}:yu_filling"},
                {"label": "清掃", "data": f"bath_cls:{record_id}:cleaning"},
                {"label": "誰もいない", "data": f"bath_cls:{record_id}:no_one"},
            ]
            try:
                sent = await asyncio.to_thread(broadcast_with_quick_reply, msg, items)
                if sent > 0:
                    await asyncio.to_thread(
                        record_pending_notification,
                        "bath_classification", f"bath_{record_id}", msg, items,
                    )
            except Exception as e:
                log.warning("bath_classification 通知失敗: %s", e)

        global _bath_detector
        _bath_detector = BathDetector(
            db_path=str(DB_PATH),
            on_candidate=_on_bath_candidate,
            get_active_person_fn=get_active_person,
        )
        log.info("BathDetector 初期化（学習データ収集モード）")

        async def _on_meter_reading(r: "MeterReading") -> None:
            await event_bus.record_event(
                source="bathroom_meter",
                event_type="reading",
                value=float(r.humidity_pct),
            )
            # bath_detector に湿度・温度を供給（候補検知時はLINE通知へ）
            try:
                await _bath_detector.feed_humidity(
                    float(r.humidity_pct), float(r.temperature_c), r.timestamp
                )
            except Exception as e:
                log.warning("bath_detector feed失敗: %s", e)

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
            on_motion=on_motion_detected,
        )
        tasks.append(asyncio.create_task(contact.run()))
        log.info("T110/T100センサー監視を開始")

    # C220 カメラ + 顔認識
    camera_ip = env.get("CAMERA_IP", "")
    if camera_ip and camera_user:
        # FaceIdentifier を初期化（顔データなしでも動く、未認識として扱う）
        face_id_obj = None
        try:
            from src.face_id import FaceIdentifier
            face_id_obj = FaceIdentifier()
            n_faces = len(getattr(face_id_obj, "_known_person_ids", []))
            log.info("FaceIdentifier 初期化（登録済み顔: %d件）", n_faces)
        except Exception as e:
            log.warning("FaceIdentifier 初期化失敗、顔認識なしで起動: %s", e)

        cam = CameraMonitor(
            cfg=CameraConfig(
                ip=camera_ip,
                username=camera_user,
                password=camera_pass,
                poll_interval=2.0,
                save_dir=Path(__file__).resolve().parent.parent / "data" / "captures",
            ),
            on_person=on_person_detected,
            face_identifier=face_id_obj,
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
