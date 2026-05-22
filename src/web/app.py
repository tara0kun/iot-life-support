"""FastAPIメインアプリ。

2つの経路を提供:
  /tablet  — 祖母用タブレット (読み取り専用、編集UIなし)
  /family  — 家族用 (認証必須、全員閲覧+編集)
  /api     — 内部API (イベント取得、WebSocket)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, time, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..db import get_conn, init_db, transaction
from ..event_bus import get_events_today, get_recent_events, get_events_by_date, subscribe, unsubscribe
from ..sessions import sessions_today, last_session
from ..garden import save_daily_score, get_garden_data, FLOWER_TYPES, _date_to_color
from ..lock_manager import get_device_state, lock_device, unlock_device
from .camera_stream import get_streamer

app = FastAPI(title="IoT生活サポート")
app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32))

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

FAMILY_PASSWORD_HASH = ""  # .envから動的に読み込み


def _load_family_password() -> str:
    env_path = BASE.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("FAMILY_PASSWORD="):
                pw = line.split("=", 1)[1].strip()
                if pw:
                    return hashlib.sha256(pw.encode()).hexdigest()
    return FAMILY_PASSWORD_HASH


@app.on_event("startup")
async def startup():
    init_db()


def _is_family_authenticated(request: Request) -> bool:
    return request.session.get("family_auth") is True


def _load_tablet_token() -> str:
    env_path = BASE.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("TABLET_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _is_local(request: Request) -> bool:
    """ローカルネットワークからのアクセスか判定。"""
    host = request.client.host if request.client else ""
    return host.startswith("192.168.") or host.startswith("10.") or host in ("127.0.0.1", "::1")


def _check_tablet_access(request: Request) -> bool:
    """タブレット画面のアクセス権チェック。ローカルはフリー、外部はトークン必須。"""
    if _is_local(request):
        return True
    token = _load_tablet_token()
    if not token:
        return True  # トークン未設定ならフリー
    # URLパラメータ or セッションでチェック
    if request.query_params.get("token") == token:
        request.session["tablet_auth"] = True
        return True
    return request.session.get("tablet_auth") is True


@app.get("/")
async def root():
    return RedirectResponse("/tablet", status_code=303)


# ============================================================
# 祖母用タブレット
# ============================================================

@app.get("/tablet", response_class=HTMLResponse)
async def tablet_view(request: Request):
    if not _check_tablet_access(request):
        return HTMLResponse("<h2 style='text-align:center;margin-top:100px;font-family:sans-serif;color:#666;'>アクセスできません</h2>", status_code=403)
    grandma_id = 1
    sessions = sessions_today(grandma_id)
    last = last_session(grandma_id)
    now = datetime.now()

    # 食事セッションのみの最後を取得（「最後に食べたのは」表示用）
    meal_labels = {"朝食", "昼食", "夕食", "間食", "おやつ"}
    last_meal = None
    for s in reversed(sessions):
        if s.get("label") in meal_labels:
            last_meal = s
            break

    minutes_since_last_meal = None
    if last_meal:
        last_time = last_meal.get("started_at")
        if isinstance(last_time, str):
            last_time = datetime.fromisoformat(last_time)
        if isinstance(last_time, datetime):
            minutes_since_last_meal = int((now - last_time).total_seconds() / 60)

    stamps = _build_stamps(sessions)
    current_activity = _current_activity(now, sessions)

    # 今日のスコアを保存
    done_count = len([s for s in stamps if s["done"]])
    done_labels = [s["label"] for s in stamps if s["done"]]
    save_daily_score(
        person_id=grandma_id,
        target_date=now.date(),
        done_count=done_count,
        total_count=len(stamps),
        details={"done": done_labels},
    )

    # 庭データ（過去14日）
    garden = get_garden_data(grandma_id, days=14)

    # 注意喚起
    alerts = _build_alerts(now, sessions, stamps, done_labels)

    # 今日の花の色
    today_flower_color = _date_to_color(now.date())

    # 今日の食事写真（祖母が自分の食事を見て思い出すため）
    today_start = now.strftime("%Y-%m-%d 00:00:00")
    conn = get_conn()
    try:
        meal_photos_today = [dict(r) for r in conn.execute(
            """SELECT p.id, p.session_id, p.file_name, p.taken_at,
                      m.label
                 FROM meal_photos p
                 LEFT JOIN meal_sessions m ON m.id = p.session_id
                WHERE p.taken_at >= ? AND p.deleted_at IS NULL
                  AND p.person_id = 1
                ORDER BY p.taken_at""",
            (today_start,),
        ).fetchall()]
    finally:
        conn.close()

    return templates.TemplateResponse(request, "tablet.html", {
        "now": now,
        "sessions": sessions,
        "session_count": len(sessions),
        "last_session": last,
        "last_meal": last_meal,
        "minutes_since_last_meal": minutes_since_last_meal,
        "stamps": stamps,
        "garden": garden,
        "time_greeting": _greeting(now),
        "current_activity": current_activity,
        "alerts": alerts,
        "today_flower_color": today_flower_color,
        "meal_photos_today": meal_photos_today,
        "done_count": done_count,
        "family_prompts": _get_active_prompts(),
    })


def _build_alerts(now: datetime, sessions: list, stamps: list, done_labels: list) -> list[dict]:
    """祖母タブレット向けのさりげない注意喚起を生成。"""
    alerts = []
    h = now.hour
    meal_labels = {"朝食", "昼食", "夕食", "間食"}
    meal_sessions = [s for s in sessions if s.get("label") in meal_labels]
    meal_count = len(meal_sessions)

    # 直近の食事から90分以内 → 「さっき たべたよ」（短期間の繰り返し防止）
    if meal_sessions:
        last_meal = meal_sessions[-1]
        last_time = last_meal.get("started_at")
        if isinstance(last_time, str):
            last_time = datetime.fromisoformat(last_time)
        if isinstance(last_time, datetime):
            minutes_ago = (now - last_time).total_seconds() / 60
            if 0 <= minutes_ago <= 90:
                alerts.append({
                    "type": "meal_recent",
                    "level": "gentle",
                    "message": "さっき食べましたよ",
                    "sub": f"{last_meal.get('label')}を{int(minutes_ago)}分前に食べました",
                    "color": "#E67E22",
                })

    # 食事3回以上 → さりげなく「よく食べた」
    if meal_count >= 3:
        alerts.append({
            "type": "meal_many",
            "level": "gentle",
            "message": "今日はよく食べましたね",
            "sub": f"今日 {meal_count}回 食べました",
            "color": "#E67E22",
        })

    # お薬リマインド（家族が設定したスケジュールに基づく）
    if "お薬" not in done_labels:
        med_schedule = _load_medicine_schedule()
        for med in med_schedule:
            if h >= med["hour"]:
                delay = h - med["hour"]
                if delay <= 1:
                    alerts.append({
                        "type": "medicine",
                        "level": "remind",
                        "message": f"{med['timing']}のお薬 飲みましたか？",
                        "sub": "",
                        "color": "#EC407A",
                    })
                else:
                    alerts.append({
                        "type": "medicine",
                        "level": "warn",
                        "message": f"{med['timing']}のお薬 まだですよ",
                        "sub": "",
                        "color": "#EC407A",
                    })
                break  # 最も近いスケジュールのみ表示

    # 一般的な時間帯での促し（指定ではなく、やさしい確認）
    if 7 <= h < 10 and "朝食" not in done_labels:
        alerts.append({
            "type": "meal_remind",
            "level": "gentle",
            "message": "朝ごはんは食べましたか？",
            "sub": "",
            "color": "#FF9800",
        })
    elif 11 <= h < 14 and "昼食" not in done_labels:
        alerts.append({
            "type": "meal_remind",
            "level": "gentle",
            "message": "お昼ごはんは食べましたか？",
            "sub": "",
            "color": "#FF9800",
        })
    elif 17 <= h < 21 and "夕食" not in done_labels:
        alerts.append({
            "type": "meal_remind",
            "level": "gentle",
            "message": "夕ごはんは食べましたか？",
            "sub": "",
            "color": "#FF9800",
        })

    if 16 <= h < 22 and "お風呂" not in done_labels:
        alerts.append({
            "type": "bath_remind",
            "level": "gentle",
            "message": "お風呂は入りましたか？",
            "sub": "",
            "color": "#29B6F6",
        })

    # センサー反応あり + ボタン未押下のチェック
    sensor_activity_map = {
        "お風呂": {"sources": {"bath_door", "bath_motion"}, "event_types": {"close", "open", "motion", "bath_end"}},
        "トイレ": {"sources": {"toilet"}, "event_types": {"open", "close"}},
    }
    conn = get_conn()
    try:
        today_start = datetime.combine(now.date(), datetime.min.time())
        for activity, rule in sensor_activity_map.items():
            if activity in done_labels:
                continue  # 既にスタンプ完了
            src_ph = ",".join(f"'{s}'" for s in rule["sources"])
            evt_ph = ",".join(f"'{e}'" for e in rule["event_types"])
            sensor_count = conn.execute(
                f"""SELECT COUNT(*) as cnt FROM events
                    WHERE started_at >= ?
                    AND source IN ({src_ph})
                    AND event_type IN ({evt_ph})""",
                (today_start,),
            ).fetchone()["cnt"]
            if sensor_count > 0:
                alerts.append({
                    "type": f"sensor_no_button_{activity}",
                    "level": "remind",
                    "message": f"{activity} しましたか？",
                    "sub": "センサーが反応しています。「できた」ボタンを押してください。",
                    "color": "#FF9800",
                })
    finally:
        conn.close()

    return alerts


def _greeting(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 10:
        return "おはようございます"
    if 10 <= h < 17:
        return "こんにちは"
    return "こんばんは"


def _load_rice_guide() -> str:
    """現在の炊飯量設定を返す。未設定なら空文字。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT amount FROM rice_guide WHERE meal = 'next' LIMIT 1").fetchone()
        return row["amount"] if row else ""
    finally:
        conn.close()


def _load_medicine_schedule() -> list[dict]:
    """薬の服用スケジュールをDBから読み込み。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT timing, hour, enabled FROM medicine_schedule WHERE enabled = 1 ORDER BY hour"
        ).fetchall()
        return [{"timing": r["timing"], "hour": r["hour"]} for r in rows]
    finally:
        conn.close()


def _get_rice_info() -> str:
    """現在設定されている炊飯量を返す。"""
    amount = _load_rice_guide()
    if not amount:
        return ""
    return f"ご飯は {amount} 炊いてね"


def _current_activity(now: datetime, sessions: list) -> dict:
    """時間帯に応じた挨拶を返す（活動の指定はしない）。"""
    h = now.hour
    rice_info = _get_rice_info()

    if 5 <= h < 10:
        return {"text": "おはようございます 🌅", "rice": rice_info}
    if 10 <= h < 17:
        return {"text": "良い一日を ☀️", "rice": rice_info}
    if 17 <= h < 21:
        return {"text": "お疲れさまです 🌇", "rice": rice_info}
    return {"text": "おやすみなさい 🌙", "rice": ""}


def _build_stamps(sessions: list) -> list[dict]:
    now = datetime.now()
    all_stamps = [
        {"icon": "🌅", "label": "起床", "done": False, "time": "", "current": False},
        {"icon": "💊", "label": "お薬", "done": False, "time": "", "current": False},
        {"icon": "🍚", "label": "朝食", "done": False, "time": "", "current": False},
        {"icon": "🍚", "label": "昼食", "done": False, "time": "", "current": False},
        {"icon": "🛁", "label": "お風呂", "done": False, "time": "", "current": False},
        {"icon": "💇", "label": "髪洗った", "done": False, "time": "", "current": False},
        {"icon": "🍚", "label": "夕食", "done": False, "time": "", "current": False},
        {"icon": "🌙", "label": "就寝", "done": False, "time": "", "current": False},
    ]
    for s in sessions:
        label = s.get("label", "")
        for stamp in all_stamps:
            if stamp["label"] == label and not stamp["done"]:
                t = s.get("started_at", "")
                if isinstance(t, datetime):
                    t = t.strftime("%H:%M")
                elif isinstance(t, str) and "T" in t:
                    t = t.split("T")[1][:5]
                stamp["done"] = True
                stamp["time"] = str(t)
                break

    # ドライヤーによる髪洗い検知（events テーブルから直接取得）
    today = now.strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        hw = conn.execute(
            """SELECT started_at FROM events
                WHERE source = 'hair_dryer' AND event_type = 'hair_wash'
                  AND started_at >= ?
                ORDER BY started_at DESC LIMIT 1""",
            (today + " 00:00:00",),
        ).fetchone()
    finally:
        conn.close()
    if hw:
        for stamp in all_stamps:
            if stamp["label"] == "髪洗った" and not stamp["done"]:
                t = hw["started_at"]
                if isinstance(t, str) and " " in t:
                    t = t.split(" ")[1][:5]
                stamp["done"] = True
                stamp["time"] = str(t)
                break

    return all_stamps


# ============================================================
# 家族ログイン
# ============================================================

@app.get("/family/login", response_class=HTMLResponse)
async def family_login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/family/login")
async def family_login(request: Request, password: str = Form(...)):
    expected = _load_family_password()
    given = hashlib.sha256(password.encode()).hexdigest()
    if given == expected:
        request.session["family_auth"] = True
        return RedirectResponse("/family", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "パスワードが違います"})


@app.get("/family/logout")
async def family_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/family/login", status_code=303)


# ============================================================
# 家族用API (認証必須)
# ============================================================

@app.get("/api/events")
async def api_events(request: Request, limit: int = 50):
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    # camera イベントは家族UIでは除外
    return [e for e in get_recent_events(limit * 2) if e.get("source") != "camera"][:limit]


@app.get("/api/sessions/{person_id}")
async def api_sessions(request: Request, person_id: int):
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    return sessions_today(person_id)


@app.get("/api/persons")
async def api_persons(request: Request):
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, name, role FROM persons").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/camera/snapshot")
async def api_camera_snapshot(request: Request):
    """最新フレームをJPEGで返す。家族認証必須。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    streamer = get_streamer()
    streamer.start()
    # 起動直後はフレーム未取得の可能性があるので待つ
    for _ in range(50):
        jpg = streamer.latest_jpeg()
        if jpg:
            break
        await asyncio.sleep(0.1)
    if not jpg:
        raise HTTPException(status_code=503, detail="camera unavailable")
    return Response(content=jpg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/camera/mjpeg")
async def api_camera_mjpeg(request: Request):
    """multipart/x-mixed-replace でMJPEG連続配信。家族認証必須。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    streamer = get_streamer()
    streamer.start()
    boundary = b"--frame"

    async def gen():
        last_ts = 0.0
        while True:
            if await request.is_disconnected():
                break
            jpg = streamer.latest_jpeg()
            ts = streamer.latest_age_seconds()
            now = asyncio.get_event_loop().time()
            if jpg and now - last_ts > 0.18:
                last_ts = now
                yield (
                    boundary + b"\r\n"
                    + b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpg)}\r\n\r\n".encode()
                    + jpg + b"\r\n"
                )
            await asyncio.sleep(0.1)

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/device-status")
async def api_device_status(request: Request):
    """デバイスのロック状態を取得。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    devices = ["rice_cooker"]
    result = {}
    for d in devices:
        state = get_device_state(d)
        result[d] = {
            "is_locked": state["is_locked"] if state else False,
            "cycle_count_today": state["cycle_count_today"] if state else 0,
            "updated_at": state["updated_at"].isoformat() if state and state.get("updated_at") else None,
        }
    return result


@app.post("/api/unlock")
async def api_unlock(request: Request):
    """家族がロックを手動解除する。全家族にLINE通知。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    device_name = body.get("device", "rice_cooker")
    reason = body.get("reason", "家族による手動解除")
    node_id = 1  # rice_cooker のMatter node_id

    success = await unlock_device(device_name, node_id, reason=reason)
    if success:
        now = datetime.now()
        with transaction() as conn:
            conn.execute(
                """INSERT INTO events(person_id, source, event_type, started_at, raw_meta)
                   VALUES(NULL, 'family_override', 'unlock', ?, ?)""",
                (now, json.dumps({"device": device_name, "reason": reason}, ensure_ascii=False)),
            )
        # 全家族にLINEで「ロック解除しました」をbroadcast
        try:
            from ..notifier import notify_device_unlocked
            await asyncio.to_thread(notify_device_unlocked, device_name, True, reason)
        except Exception as e:
            _webhook_log.warning("unlock通知失敗: %s", e)
        return {"ok": True, "message": "解除しました"}
    else:
        raise HTTPException(status_code=500, detail="Matter通信に失敗しました")


@app.post("/api/lock")
async def api_lock(request: Request):
    """家族が手動でロックをかける（予防的措置）。全家族にLINE通知。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    device_name = body.get("device", "rice_cooker")
    reason = body.get("reason", "家族による手動ロック")
    node_id = 1

    success = await lock_device(device_name, node_id, reason=reason)
    if success:
        now = datetime.now()
        with transaction() as conn:
            conn.execute(
                """INSERT INTO events(person_id, source, event_type, started_at, raw_meta)
                   VALUES(NULL, 'family_override', 'lock', ?, ?)""",
                (now, json.dumps({"device": device_name, "reason": reason}, ensure_ascii=False)),
            )
        # 全家族にLINEで「手動ロックしました」をactionable broadcast
        try:
            from ..notifier import notify_device_locked
            await asyncio.to_thread(notify_device_locked, device_name, True, reason)
        except Exception as e:
            _webhook_log.warning("lock通知失敗: %s", e)
        return {"ok": True, "message": "ロックしました"}
    else:
        raise HTTPException(status_code=500, detail="Matter通信に失敗しました")


# センサー照合マッピング: ボタン→どのセンサーを確認するか
SENSOR_VERIFY = {
    "起床": {"sources": ["camera"], "event_types": ["person_detected"], "window_minutes": 60},
    "お薬": None,  # センサーなし → 常に家族確認
    "お風呂": {"sources": ["bath_door", "bath_motion"], "event_types": ["close", "open", "motion", "bath_end"], "window_minutes": 120},
    "髪洗った": {"sources": ["hair_dryer"], "event_types": ["power_on", "hair_wash"], "window_minutes": 120},
    "就寝": None,  # センサーなし → 常に家族確認
}


def _verify_sensor(activity: str, person_id: int) -> dict:
    """ボタン押下時にセンサー記録を照合する。

    返却される reason:
      - 'no_sensor'              : お薬/就寝など、そもそもセンサーがない項目
      - 'sensor_confirmed'       : 直近 window 以内にセンサー反応あり（祖母として）
      - 'sensor_confirmed_unidentified' : 同上だが person_id 未識別（誰か）
      - 'already_done_old'       : 今日の window 外にセンサー反応あり → 既にやってる
      - 'no_sensor_data'         : 今日センサー反応一切なし → 本当にやった？
    """
    rule = SENSOR_VERIFY.get(activity)
    if rule is None:
        return {"verified": False, "reason": "no_sensor",
                "message": "センサーがない項目です。家族に確認してもらってください。"}

    window = timedelta(minutes=rule["window_minutes"])
    now = datetime.now()
    since = now - window
    today_start = datetime.combine(now.date(), time.min)

    conn = get_conn()
    try:
        placeholders_src = ",".join("?" for _ in rule["sources"])
        placeholders_evt = ",".join("?" for _ in rule["event_types"])

        # window内にセンサー反応あり（祖母 person_id一致）
        params = [person_id, since] + rule["sources"] + rule["event_types"]
        row = conn.execute(
            f"""SELECT COUNT(*) as cnt FROM events
                WHERE person_id = ? AND started_at >= ?
                AND source IN ({placeholders_src})
                AND event_type IN ({placeholders_evt})""",
            params,
        ).fetchone()
        if row and row["cnt"] > 0:
            return {"verified": True, "reason": "sensor_confirmed",
                    "message": "センサーで確認できました。"}

        # window内に person_id 不問のセンサー反応あり
        params2 = [since] + rule["sources"] + rule["event_types"]
        row2 = conn.execute(
            f"""SELECT COUNT(*) as cnt FROM events
                WHERE started_at >= ?
                AND source IN ({placeholders_src})
                AND event_type IN ({placeholders_evt})""",
            params2,
        ).fetchone()
        if row2 and row2["cnt"] > 0:
            return {"verified": True, "reason": "sensor_confirmed_unidentified",
                    "message": "センサーで確認できました。"}

        # 今日のwindow外にセンサー反応がある場合 → 既にやってる
        params3 = [today_start] + rule["sources"] + rule["event_types"]
        row3 = conn.execute(
            f"""SELECT MAX(started_at) as last_at FROM events
                WHERE started_at >= ?
                AND source IN ({placeholders_src})
                AND event_type IN ({placeholders_evt})""",
            params3,
        ).fetchone()
        if row3 and row3["last_at"]:
            last_at = row3["last_at"]
            # 時刻表示用に HH:MM 取り出し
            time_str = ""
            if isinstance(last_at, str):
                if " " in last_at:
                    time_str = last_at.split(" ")[1][:5]
                elif "T" in last_at:
                    time_str = last_at.split("T")[1][:5]
            return {"verified": False, "reason": "already_done_old",
                    "last_time": time_str,
                    "message": f"もうすでに{activity}できてますよ。{time_str}にできていました。"}

        return {"verified": False, "reason": "no_sensor_data",
                "message": "センサーが感知していません。家族に聞いてください。"}
    finally:
        conn.close()


TABLET_COOLDOWN_MINUTES = 30


def _check_cooldown(activity: str, person_id: int) -> bool:
    """同じ活動のボタンが最近押されていないかチェック。True=押せる、False=クールダウン中。"""
    conn = get_conn()
    try:
        since = datetime.now() - timedelta(minutes=TABLET_COOLDOWN_MINUTES)
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM events
               WHERE person_id = ? AND source = 'tablet_report'
               AND event_type IN (?, ?) AND started_at >= ?""",
            (person_id, activity, f"{activity}_unverified", since),
        ).fetchone()
        return row["cnt"] == 0
    finally:
        conn.close()


@app.post("/api/tablet-record")
async def api_tablet_record(request: Request):
    """祖母がタブレットの「できた」ボタンを押したときの処理。"""
    # ローカル or タブレットセッション認証済みのみ許可
    if not _is_local(request) and not _check_tablet_access(request):
        raise HTTPException(status_code=403, detail="アクセスできません")
    body = await request.json()
    activity = body.get("activity", "")
    person_id = body.get("person_id", 1)

    valid = {"起床", "お薬", "お風呂", "髪洗った", "就寝"}
    if activity not in valid:
        raise HTTPException(status_code=400, detail=f"無効な活動: {activity}")

    now = datetime.now()

    # クールダウンチェック
    if not _check_cooldown(activity, person_id):
        return {"ok": False, "verified": False, "reason": "cooldown",
                "message": f"さっき押しましたよ。{TABLET_COOLDOWN_MINUTES}分後にまた押せます。"}

    # センサー照合
    verify = _verify_sensor(activity, person_id)

    reason = verify["reason"]

    # LINE通知（ケース別）
    try:
        from ..notifier import send_line_message, send_actionable_notification
        import asyncio
        ctx_key = f"{now.strftime('%Y-%m-%d_%H%M%S')}_{activity}"
        if verify["verified"]:
            # センサー確認済み = 情報通知（admin限定）
            msg = f"📋 祖母が「{activity}」ボタンを押しました\n✅ センサー確認済み\n時刻: {now.strftime('%H:%M')}"
            await asyncio.to_thread(send_line_message, msg)
        elif reason == "already_done_old":
            # 既にセンサーで確認済（窓外） = 情報通知のみ、家族の対応不要
            last_t = verify.get("last_time", "")
            msg = (f"📋 祖母が「{activity}」ボタンを押しました\n"
                   f"ℹ️ 既に{last_t}にセンサー確認済（重複押下、記録はしません）\n"
                   f"時刻: {now.strftime('%H:%M')}")
            await asyncio.to_thread(send_line_message, msg)
        elif reason == "no_sensor":
            # センサーなし（お薬・就寝） → 家族確認必要 = アクション付き
            msg = f"📋 祖母が「{activity}」ボタンを押しました\n⚠️ センサーなし（家族確認が必要）\n時刻: {now.strftime('%H:%M')}"
            await asyncio.to_thread(
                send_actionable_notification,
                "tablet_unverified", ctx_key, msg,
            )
        else:
            # no_sensor_data → センサーが感知してないので家族確認必要
            msg = (f"📋 祖母が「{activity}」ボタンを押しました\n"
                   f"❌ センサーが感知していません（家族の確認が必要）\n"
                   f"時刻: {now.strftime('%H:%M')}")
            await asyncio.to_thread(
                send_actionable_notification,
                "tablet_unverified", ctx_key, msg,
            )
    except Exception:
        pass

    if verify["verified"]:
        # センサー確認済 → 記録する
        with transaction() as conn:
            conn.execute(
                """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
                   VALUES(?, 'tablet_report', ?, ?, NULL, 1.0, ?)""",
                (person_id, activity, now,
                 json.dumps({"verified": True, "verify_reason": reason}, ensure_ascii=False)),
            )
            conn.execute(
                """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
                   VALUES(?, ?, ?, 1, ?)""",
                (person_id, now, now, activity),
            )
        return {"ok": True, "verified": True, "reason": reason,
                "message": f"{activity} を記録しました"}

    if reason == "already_done_old":
        # 既にセンサー検知済（窓外） → 重複記録しない、祖母にお知らせ
        return {"ok": False, "verified": False, "reason": "already_done_old",
                "last_time": verify.get("last_time", ""),
                "activity": activity,
                "message": verify["message"]}

    # 未確認 → unverified として記録（家族確認用）
    with transaction() as conn:
        conn.execute(
            """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
               VALUES(?, 'tablet_report', ?, ?, NULL, 0.0, ?)""",
            (person_id, f"{activity}_unverified", now,
             json.dumps({"verified": False, "verify_reason": reason}, ensure_ascii=False)),
        )
    return {"ok": False, "verified": False, "reason": reason,
            "activity": activity,
            "message": verify["message"]}


# ============================================================
# 食事写真撮影機能
# ============================================================

MEAL_PHOTOS_DIR = BASE.parent.parent / "data" / "meal_photos"
MEAL_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
MEAL_PHOTO_PROMPT_WINDOW_MIN = 60   # 食事検知後この分数内なら写真撮影プロンプトを出す
MEAL_LABELS_FOR_PHOTO = {"朝食", "昼食", "夕食", "間食", "おやつ"}


def _current_tunnel_base_url() -> str:
    """LINE 画像送信に使う公開URL の base。Cloudflare TunnelのURL。"""
    url_file = BASE.parent.parent / "data" / "tunnel_url.txt"
    if url_file.exists():
        return url_file.read_text().strip().rstrip("/")
    return ""


@app.get("/api/meal-photo-prompt")
async def api_meal_photo_prompt(request: Request):
    """祖母タブレット用: 直近の食事セッションで写真未撮影なら情報を返す。"""
    if not _is_local(request) and not _check_tablet_access(request):
        raise HTTPException(status_code=403)
    cutoff = (datetime.now() - timedelta(minutes=MEAL_PHOTO_PROMPT_WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" * len(MEAL_LABELS_FOR_PHOTO))
    conn = get_conn()
    try:
        row = conn.execute(
            f"""SELECT m.id as session_id, m.label, m.started_at,
                       (SELECT COUNT(*) FROM meal_photos p
                          WHERE p.session_id = m.id AND p.deleted_at IS NULL) as photo_count
                  FROM meal_sessions m
                 WHERE m.person_id = 1
                   AND m.started_at >= ?
                   AND m.label IN ({placeholders})
                 ORDER BY m.started_at DESC
                 LIMIT 1""",
            (cutoff, *MEAL_LABELS_FOR_PHOTO),
        ).fetchone()
    finally:
        conn.close()
    if not row or row["photo_count"] > 0:
        return {"prompt": False}
    started = row["started_at"]
    if isinstance(started, str) and " " in started:
        time_str = started.split(" ")[1][:5]
    else:
        time_str = ""
    return {
        "prompt": True,
        "session_id": row["session_id"],
        "label": row["label"],
        "time": time_str,
    }


@app.post("/api/meal-photo")
async def api_upload_meal_photo(
    request: Request,
    photo: UploadFile = File(...),
    session_id: int = Form(...),
):
    """祖母タブレットから食事写真をアップロードする。

    保存先: data/meal_photos/{session_id}_{timestamp}.jpg
    LINE で全家族に画像をbroadcast、DB に記録。
    """
    if not _is_local(request) and not _check_tablet_access(request):
        raise HTTPException(status_code=403, detail="アクセスできません")
    data = await photo.read()
    if not data:
        raise HTTPException(status_code=400, detail="画像データが空です")
    if len(data) > 5 * 1024 * 1024:  # 5MB上限
        raise HTTPException(status_code=400, detail="画像が大きすぎます（5MB以下）")

    now = datetime.now()
    fname = f"{session_id}_{int(now.timestamp())}.jpg"
    fpath = MEAL_PHOTOS_DIR / fname
    fpath.write_bytes(data)

    # DB登録
    conn = get_conn()
    try:
        sess = conn.execute(
            "SELECT id, person_id, label FROM meal_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if not sess:
        fpath.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail="セッションが見つかりません")

    with transaction() as conn:
        conn.execute(
            """INSERT INTO meal_photos(session_id, person_id, file_name, file_size, taken_at)
               VALUES(?, ?, ?, ?, ?)""",
            (session_id, sess["person_id"], fname, len(data), now),
        )

    # LINE で全家族に broadcast
    base = _current_tunnel_base_url()
    if base:
        public_url = f"{base}/photos/{fname}"
        try:
            from ..notifier import broadcast_line_image
            label = sess["label"] or "食事"
            time_str = now.strftime("%H:%M")
            caption = f"🍚 祖母が{label}の写真を撮りました\n時刻: {time_str}"
            await asyncio.to_thread(broadcast_line_image, public_url, public_url, caption)
        except Exception as e:
            _webhook_log.warning("食事写真LINE送信失敗: %s", e)

    return {"ok": True, "file_name": fname, "session_id": session_id}


# ============================================================
# 使い方ガイド（外部公開、認証なし — 家族間で共有可能）
# ============================================================

GUIDE_DIR = BASE.parent.parent / "docs" / "guide"

GUIDE_PAGES = {
    "": ("📚 ガイド一覧", "README.md"),
    "daily-usage": ("🌟 毎日の使い方（家族向け簡略版）", "daily-usage.md"),
    "tablet-setup": ("祖母タブレット セットアップ手順", "tablet-setup.md"),
    "grandma-usage": ("祖母用 タブレットの使い方", "grandma-usage.md"),
    "family-setup": ("家族用 セットアップ手順", "family-setup.md"),
    "family-reference": ("家族用 機能リファレンス", "family-reference.md"),
    "line-operation": ("LINE操作ガイド", "line-operation.md"),
    "troubleshooting": ("トラブルシューティング", "troubleshooting.md"),
}


@app.get("/guide/", response_class=HTMLResponse)
@app.get("/guide", response_class=HTMLResponse)
async def guide_index(request: Request):
    return await _render_guide(request, "")


@app.get("/guide/{slug}", response_class=HTMLResponse)
async def guide_page(request: Request, slug: str):
    return await _render_guide(request, slug)


async def _render_guide(request: Request, slug: str) -> HTMLResponse:
    # README.md など .md 付きリンクで来るケースに対応（slug=tablet-setup.md → tablet-setup）
    if slug.endswith(".md"):
        slug = slug[:-3]
    if slug not in GUIDE_PAGES:
        raise HTTPException(status_code=404, detail="ガイドが見つかりません")
    title, fname = GUIDE_PAGES[slug]
    fpath = GUIDE_DIR / fname
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="ガイドファイルが存在しません")
    md_text = fpath.read_text(encoding="utf-8")
    try:
        import markdown as _md
        html_content = _md.markdown(
            md_text,
            extensions=["tables", "fenced_code", "toc", "sane_lists"],
        )
    except Exception as e:
        html_content = f"<pre>ガイドのレンダリングに失敗しました: {e}\n\n{md_text}</pre>"
    return templates.TemplateResponse(request, "guide.html", {
        "title": title,
        "html_content": html_content,
    })


@app.get("/photos/{file_name}")
async def serve_meal_photo(request: Request, file_name: str):
    """食事写真を配信する（LINEサーバーからの取得用、外部公開）。

    ファイル名のサニタイズで path traversal を防止。
    """
    if "/" in file_name or ".." in file_name:
        raise HTTPException(status_code=400)
    fpath = MEAL_PHOTOS_DIR / file_name
    if not fpath.exists():
        raise HTTPException(status_code=404)
    return FileResponse(fpath, media_type="image/jpeg")


@app.get("/api/meal-photos")
async def api_list_meal_photos(request: Request, days: int = 7):
    """家族UI: 直近の食事写真を取得。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    days = max(1, min(days, 30))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT p.id, p.session_id, p.file_name, p.taken_at,
                      m.label, m.started_at as session_start, per.name as person_name
                 FROM meal_photos p
                 LEFT JOIN meal_sessions m ON m.id = p.session_id
                 LEFT JOIN persons per ON per.id = p.person_id
                WHERE p.taken_at >= ? AND p.deleted_at IS NULL
                ORDER BY p.taken_at DESC""",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/meal-photos/{photo_id}")
async def api_delete_meal_photo(request: Request, photo_id: int):
    """家族UIから食事写真を論理削除。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    with transaction() as conn:
        conn.execute(
            "UPDATE meal_photos SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
            (photo_id,),
        )
    return {"ok": True}


@app.post("/api/quick-record")
async def api_quick_record(request: Request):
    """家族が証人として祖母の行動を記録する。

    activity:
      - 起床/お薬/就寝/お風呂/トイレ/おやつ: 単発活動
      - 外食: 家族が一緒に外食したことを記録（食事カウント+1扱い）
    """
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    activity = body.get("activity", "")
    person_id = body.get("person_id", 1)  # デフォルト: 祖母
    witness = body.get("witness", "家族")
    meal_kind = body.get("meal_kind", "")  # 朝食/昼食/夕食/間食 — 外食時のみ使用

    valid_activities = {"起床", "お薬", "就寝", "お風呂", "トイレ", "おやつ",
                        "外食", "朝食", "昼食", "夕食", "間食"}
    if activity not in valid_activities:
        raise HTTPException(status_code=400, detail=f"無効な活動: {activity}")

    now = datetime.now()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
               VALUES(?, 'family_report', ?, ?, NULL, 1.0, ?)""",
            (person_id, activity, now, json.dumps(
                {"witness": witness, "meal_kind": meal_kind} if meal_kind else {"witness": witness},
                ensure_ascii=False,
            )),
        )

    # 単発活動はセッション化（食事カウントとは別系統）
    if activity in {"起床", "お薬", "就寝", "お風呂", "トイレ"}:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
                   VALUES(?, ?, ?, 1, ?)""",
                (person_id, now, now, activity),
            )

    # 外食: 食事として meal_sessions に登録（食事カウントに含まれる）
    if activity == "外食":
        # meal_kind が指定されてなければ時間帯から推定
        if not meal_kind:
            h = now.hour
            if 5 <= h < 10:
                meal_kind = "朝食"
            elif 10 <= h < 15:
                meal_kind = "昼食"
            elif 15 <= h < 21:
                meal_kind = "夕食"
            else:
                meal_kind = "間食"
        with transaction() as conn:
            conn.execute(
                """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
                   VALUES(?, ?, ?, 1, ?)""",
                (person_id, now, now, f"外食({meal_kind})"),
            )

    # 通常の食事（朝食/昼食/夕食/間食）: 家族が手動で記録（センサ見逃し時など）
    if activity in {"朝食", "昼食", "夕食", "間食"}:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
                   VALUES(?, ?, ?, 1, ?)""",
                (person_id, now, now, activity),
            )

    # 家族が手動で記録した内容は全員にbroadcast（重複記録防止＋情報共有）
    try:
        from ..notifier import broadcast_line_message
        if activity == "外食":
            await asyncio.to_thread(
                broadcast_line_message,
                f"📝 家族が「外食({meal_kind})」を記録しました（{now.strftime('%H:%M')}）",
            )
        else:
            await asyncio.to_thread(
                broadcast_line_message,
                f"📝 家族が「{activity}」を記録しました（{now.strftime('%H:%M')}）",
            )
    except Exception as e:
        logging.getLogger("app").warning("quick-record broadcast失敗: %s", e)

    return {"ok": True, "activity": activity, "time": now.strftime("%H:%M"),
            "meal_kind": meal_kind if activity == "外食" else None}


@app.post("/api/family-prompt")
async def api_send_prompt(request: Request):
    """家族から祖母タブレットにメッセージを送る。

    priority:
      - 'critical': 音声強調＋繰り返し読み上げ
      - 'normal'  : 通常（音声・表示）
      - 'silent'  : 表示のみ、音声なし
    """
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    message = body.get("message", "").strip()
    minutes = body.get("minutes", 60)
    priority = body.get("priority", "normal")
    if priority not in ("critical", "normal", "silent"):
        priority = "normal"
    if not message:
        raise HTTPException(status_code=400, detail="メッセージを入力してください")
    now = datetime.now()
    expires = now + timedelta(minutes=int(minutes))
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_str = expires.strftime("%Y-%m-%d %H:%M:%S")
    with transaction() as conn:
        conn.execute(
            "INSERT INTO family_prompts(message, sent_by, created_at, expires_at, priority) VALUES(?, ?, ?, ?, ?)",
            (message, "家族", now_str, expires_str, priority),
        )
    # event_bus 経由でWebSocket購読者（タブレット）に即時通知
    # タブレット側のWS handlerがリロードを実行 → 新しいfamily_promptが即座に表示される
    from ..event_bus import _notify
    await _notify({
        "source": "family_prompt",
        "event_type": "new",
        "value": None,
        "person_id": None,
        "started_at": now_str,
        "id": None,
        "_payload": {"message": message, "priority": priority},
    })
    return {"ok": True, "message": message, "expires_at": expires_str, "priority": priority}


@app.post("/api/dismiss-prompt/{prompt_id}")
async def api_dismiss_prompt(request: Request, prompt_id: int):
    """祖母がメッセージを確認済みにする。"""
    with transaction() as conn:
        conn.execute("UPDATE family_prompts SET dismissed = 1 WHERE id = ?", (prompt_id,))
    return {"ok": True}


def _get_active_prompts() -> list[dict]:
    """有効な家族メッセージを取得。"""
    conn = get_conn()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            """SELECT id, message, sent_by, created_at, priority FROM family_prompts
               WHERE dismissed = 0 AND expires_at > ?
               ORDER BY created_at DESC""",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================
# LINE Webhook (「リンクが欲しい」等のメッセージで現URLを返信)
# ============================================================

_webhook_log = logging.getLogger("line_webhook")

LINE_URL_TRIGGERS = (
    "リンク", "url", "URL", "Url",
    "つながらない", "繋がらない", "つながらん",
    "見れない", "みれない", "見られない", "みられない",
    "アクセス", "開けない", "あけない",
    "接続",
)


def _load_line_secret() -> str:
    env_path = BASE.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("LINE_CHANNEL_SECRET="):
                return line.split("=", 1)[1].strip()
    return ""


def _load_line_allowed_senders() -> set[str]:
    """許可された送信者ID。.env と DB(family_line_users) の両方を統合。

    家族登録機能 (登録 母 等) で増えるDB上のIDもここで読まれて、
    iot-web 再起動なしに即座に許可送信者として扱われる。
    """
    env_path = BASE.parent.parent / ".env"
    allowed: set[str] = set()
    line_user_id = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("LINE_ALLOWED_SENDERS="):
                raw = line.split("=", 1)[1].strip()
                allowed.update(x.strip() for x in raw.split(",") if x.strip())
            elif line.startswith("LINE_USER_ID="):
                line_user_id = line.split("=", 1)[1].strip()
    if line_user_id:
        allowed.add(line_user_id)
    # DBから家族登録者も追加
    try:
        from ..db import get_conn
        conn = get_conn()
        try:
            for r in conn.execute("SELECT line_user_id FROM family_line_users").fetchall():
                if r["line_user_id"]:
                    allowed.add(r["line_user_id"])
        finally:
            conn.close()
    except Exception:
        pass
    return allowed


def _current_tunnel_url() -> str:
    url_file = BASE.parent.parent / "data" / "tunnel_url.txt"
    if url_file.exists():
        return url_file.read_text().strip()
    return ""


def _build_url_reply() -> str:
    url = _current_tunnel_url()
    if not url:
        return "⚠️ 現在公開URLが未発行です。ラズパイ側で `bash scripts/start_tunnel.sh` を実行してください。"
    token = _load_tablet_token()
    tablet_url = f"{url}/tablet?token={token}" if token else f"{url}/tablet"
    return (
        "🌐 最新の公開URL\n\n"
        "📱 タブレット画面:\n"
        f"{tablet_url}\n\n"
        "👨‍👩‍👧 家族管理画面:\n"
        f"{url}/family"
    )


def _is_url_request(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    for kw in LINE_URL_TRIGGERS:
        if kw.lower() in lower:
            return True
    return False


def _extract_sender_id(source: dict) -> str:
    """LINE event.source からIDを取り出す（group/room/user のいずれか）。"""
    return source.get("groupId") or source.get("roomId") or source.get("userId", "")


@app.get("/line/webhook")
async def line_webhook_get():
    """LINE Developersコンソールからの疎通確認用（GETには200で応答）。"""
    return PlainTextResponse("OK")


@app.post("/line/webhook")
async def line_webhook(request: Request):
    """LINE Messaging APIからのwebhookを受信する。

    「リンク」「URL」等のキーワードを含むメッセージを受けたら、現在の公開URLを返信する。
    """
    body = await request.body()
    signature = request.headers.get("x-line-signature", "")
    secret = _load_line_secret()

    # 署名検証（シークレット未設定なら検証スキップ＝自己責任）
    if secret:
        expected = base64.b64encode(
            hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
        ).decode("utf-8")
        if not hmac.compare_digest(expected, signature):
            _webhook_log.warning("LINE webhook署名不一致")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    allowed = _load_line_allowed_senders()

    from ..notifier import reply_line_message
    from ..line_commands import (
        dispatch, handle_attribute_postback, handle_merge_postback,
        handle_confirm_postback, handle_confirm_dismiss_postback,
        handle_rice_action_postback,
        handle_bath_classification_postback,
        handle_lock_confirm_postback,
        handle_session_confirm_postback,
    )
    events = payload.get("events", [])
    for ev in events:
        ev_type = ev.get("type", "")
        sender_id = _extract_sender_id(ev.get("source", {}))
        reply_token = ev.get("replyToken", "")

        # 全受信メッセージのsender_idをログ（家族追加時のID取得用）
        print(f"RECV type={ev_type} sender={sender_id}", flush=True)

        # メッセージ本文を先に取得（登録コマンド判定のため）
        text_for_check = ""
        if ev_type == "message":
            m = ev.get("message", {})
            if m.get("type") == "text":
                text_for_check = m.get("text", "").strip()
        is_register_command = text_for_check.startswith("登録")

        # 許可リストチェック（登録コマンドだけは未登録者でも通す）
        if allowed and sender_id not in allowed and not is_register_command:
            _webhook_log.info("未許可送信者: %s", sender_id)
            continue

        # postback (Quick Reply ボタン押下) の処理
        if ev_type == "postback":
            data = ev.get("postback", {}).get("data", "")
            reply: str | None = None
            try:
                if data.startswith("attribute:"):
                    reply = await handle_attribute_postback(data, sender_id)
                elif data.startswith("merge:"):
                    reply = await handle_merge_postback(data, sender_id)
                elif data.startswith("confirm:"):
                    reply = await handle_confirm_postback(data, sender_id)
                elif data.startswith("confirm_dismiss:"):
                    reply = await handle_confirm_dismiss_postback(data, sender_id)
                elif data.startswith("rice_action:"):
                    reply = await handle_rice_action_postback(data, sender_id)
                elif data.startswith("bath_cls:"):
                    reply = await handle_bath_classification_postback(data, sender_id)
                elif data.startswith("lock_confirm:"):
                    reply = await handle_lock_confirm_postback(data, sender_id)
                elif data.startswith("sess_confirm:"):
                    reply = await handle_session_confirm_postback(data, sender_id)
                elif data == "feedback_start":
                    reply = (
                        "💬 意見・質問の送り方\n\n"
                        "「意見 ＜内容＞」と書いてメッセージを送ってください。\n\n"
                        "例:\n"
                        "意見 ご飯写真がうまく送れません\n"
                        "質問 通知を減らせますか？\n"
                        "要望 〇〇という機能がほしい\n"
                        "バグ 画面が真っ白になる"
                    )
            except Exception as e:
                _webhook_log.error("postback処理エラー: %s", e)
                reply = "⚠️ 処理に失敗しました"
            if reply:
                try:
                    await asyncio.to_thread(reply_line_message, reply_token, reply)
                except Exception as e:
                    _webhook_log.error("LINE返信失敗: %s", e)
            continue

        # message タイプの処理
        if ev_type != "message":
            continue
        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue
        text = msg.get("text", "")

        # コマンドディスパッチ（リンク以外すべて）
        reply = await dispatch(text, sender_id)
        if reply is None and _is_url_request(text):
            reply = _build_url_reply()

        if isinstance(reply, dict) and reply.get("_type") == "menu":
            # メニュー: Quick Reply 形式で送信（URI と postback 混在）
            from ..notifier import send_line_with_quick_reply
            menu_text = reply.get("text", "メニュー")
            menu_items = reply.get("items", [])
            try:
                await asyncio.to_thread(
                    send_line_with_quick_reply,
                    menu_text, menu_items, user_id=sender_id,
                )
            except Exception as e:
                _webhook_log.error("メニュー送信失敗: %s", e)
        elif reply:
            try:
                await asyncio.to_thread(reply_line_message, reply_token, reply)
            except Exception as e:
                _webhook_log.error("LINE返信失敗: %s", e)

    # 常に200を返す（LINEの再送を防ぐ）
    return {"ok": True}


@app.get("/family/manual", response_class=HTMLResponse)
async def family_manual(request: Request):
    """家族向け説明書＋操作マニュアル（ブラウザ印刷でPDF化可）。"""
    if not _is_family_authenticated(request):
        return RedirectResponse("/family/login", status_code=303)
    return templates.TemplateResponse(request, "family_manual.html", {})


@app.get("/family/weekly-report", response_class=HTMLResponse)
async def family_weekly_report(request: Request):
    """週次レポートをブラウザで表示。Chrome等の「PDFに保存」で出力できる。"""
    if not _is_family_authenticated(request):
        return RedirectResponse("/family/login", status_code=303)
    end_date_str = request.query_params.get("end", "")
    days = int(request.query_params.get("days", "7"))
    days = max(1, min(days, 30))
    if end_date_str:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    else:
        end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days - 1)
    date_strs = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    grandma_id = 1
    meal_labels = {"朝食", "昼食", "夕食", "間食", "おやつ"}
    stamp_labels = ["起床", "お薬", "朝食", "昼食", "お風呂", "夕食", "就寝"]

    conn = get_conn()
    try:
        ph = ",".join("?" * len(date_strs))
        # 食事
        meal_rows = conn.execute(
            f"""SELECT DATE(started_at) as d, label, started_at FROM meal_sessions
                WHERE person_id = ? AND DATE(started_at) IN ({ph})
                AND label IN ('朝食','昼食','夕食','間食','おやつ')
                ORDER BY started_at""",
            (grandma_id, *date_strs),
        ).fetchall()
        # スタンプ
        stamp_rows = conn.execute(
            f"""SELECT date, done_count, total_count, details FROM daily_scores
                WHERE person_id = ? AND date IN ({ph})""",
            (grandma_id, *date_strs),
        ).fetchall()
        # お薬
        med_taken = conn.execute(
            f"""SELECT DATE(started_at) as d, COUNT(*) as cnt FROM events
                WHERE source IN ('family_report', 'tablet_report')
                AND event_type = 'お薬'
                AND DATE(started_at) IN ({ph})
                GROUP BY d""",
            date_strs,
        ).fetchall()
        med_sched = conn.execute(
            "SELECT COUNT(*) as cnt FROM medicine_schedule WHERE enabled = 1"
        ).fetchone()["cnt"]
        # ロック発動
        lock_count = conn.execute(
            f"""SELECT COUNT(*) as cnt FROM events
                WHERE source = 'lock_manager' AND event_type = 'auto_lock'
                AND DATE(started_at) IN ({ph})""",
            date_strs,
        ).fetchone()["cnt"]
    finally:
        conn.close()

    # 集計
    by_date = {ds: {"meals": [], "stamps": 0, "med_taken": 0} for ds in date_strs}
    for r in meal_rows:
        t = r["started_at"]
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("T", " "))
            except ValueError:
                continue
        by_date.setdefault(r["d"], {"meals": [], "stamps": 0, "med_taken": 0})
        by_date[r["d"]]["meals"].append({"label": r["label"], "time": t.strftime("%H:%M")})
    for r in stamp_rows:
        if r["date"] in by_date:
            by_date[r["date"]]["stamps"] = r["done_count"]
    for r in med_taken:
        if r["d"] in by_date:
            by_date[r["d"]]["med_taken"] = r["cnt"]

    daily_data = [{"date": ds, **by_date[ds]} for ds in date_strs]

    meal_counts = [len(d["meals"]) for d in daily_data]
    stamp_counts = [d["stamps"] for d in daily_data]
    med_counts = [d["med_taken"] for d in daily_data]

    summary = {
        "meal_avg": sum(meal_counts) / len(meal_counts) if meal_counts else 0,
        "meal_max": max(meal_counts) if meal_counts else 0,
        "overeat_days": sum(1 for c in meal_counts if c >= 3),
        "stamp_avg": sum(stamp_counts) / len(stamp_counts) if stamp_counts else 0,
        "stamp_pct": int(100 * sum(stamp_counts) / (len(stamp_labels) * len(date_strs))) if date_strs else 0,
        "med_pct": int(100 * sum(med_counts) / (med_sched * len(date_strs))) if med_sched else 0,
        "lock_count": lock_count,
    }

    return templates.TemplateResponse(request, "weekly_report.html", {
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "daily_data": daily_data,
        "summary": summary,
        "stamp_labels": stamp_labels,
    })


# ============================================================
# 顔学習（受動収集 + 遠隔ラベル付け）
# ============================================================

@app.get("/api/face-candidates")
async def api_face_candidates(request: Request):
    """未識別顔の候補一覧（カメラが自動収集したもの）"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    from ..face_id import CANDIDATES_INDEX, CANDIDATES_DIR
    if not CANDIDATES_INDEX.exists():
        return {"candidates": []}
    try:
        index = json.loads(CANDIDATES_INDEX.read_text())
    except Exception:
        index = []
    # 存在チェック + 新しい順
    valid = [c for c in index if (CANDIDATES_DIR / c["file"]).exists()]
    valid.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return {"candidates": valid[:200]}


@app.get("/face-candidates/{file_name}")
async def serve_face_candidate(request: Request, file_name: str):
    """候補顔画像を配信（家族UI内で表示用、認証必須）"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    from ..face_id import CANDIDATES_DIR
    if "/" in file_name or ".." in file_name:
        raise HTTPException(status_code=400)
    fpath = CANDIDATES_DIR / file_name
    if not fpath.exists():
        raise HTTPException(status_code=404)
    return FileResponse(fpath, media_type="image/jpeg")


@app.post("/api/face-candidates/label")
async def api_face_candidate_label(request: Request):
    """候補顔をラベル付けして既存 person に encoding を追加。

    body: {"file": "...", "person_id": int, "name": str}
    """
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    file_name = body.get("file", "")
    person_id = body.get("person_id")
    name = body.get("name", "").strip()
    if not file_name or not person_id or not name:
        raise HTTPException(status_code=400, detail="file, person_id, name 必須")

    from ..face_id import CANDIDATES_INDEX, CANDIDATES_DIR, FaceIdentifier
    if not CANDIDATES_INDEX.exists():
        raise HTTPException(status_code=404, detail="候補リストなし")
    try:
        index = json.loads(CANDIDATES_INDEX.read_text())
    except Exception:
        raise HTTPException(status_code=500, detail="候補リスト破損")

    target = next((c for c in index if c["file"] == file_name), None)
    if not target:
        raise HTTPException(status_code=404, detail="候補顔が見つかりません")

    # FaceIdentifier に登録
    fid = FaceIdentifier()
    success = fid.register_from_encoding(int(person_id), name, target["encoding"])
    if not success:
        raise HTTPException(status_code=500, detail="登録失敗")

    # 候補リストから削除＋画像も削除（学習に使ったので）
    index = [c for c in index if c["file"] != file_name]
    try:
        CANDIDATES_INDEX.write_text(json.dumps(index, ensure_ascii=False))
        (CANDIDATES_DIR / file_name).unlink(missing_ok=True)
    except Exception:
        pass

    return {"ok": True, "person_id": person_id, "name": name,
            "registered_count": fid.registered_count}


@app.post("/api/face-candidates/dismiss")
async def api_face_candidate_dismiss(request: Request):
    """候補顔を「学習対象外」として削除（不審者・通行人など）"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    file_name = body.get("file", "")
    if not file_name:
        raise HTTPException(status_code=400)
    from ..face_id import CANDIDATES_INDEX, CANDIDATES_DIR
    try:
        index = json.loads(CANDIDATES_INDEX.read_text()) if CANDIDATES_INDEX.exists() else []
        index = [c for c in index if c["file"] != file_name]
        CANDIDATES_INDEX.write_text(json.dumps(index, ensure_ascii=False))
        (CANDIDATES_DIR / file_name).unlink(missing_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.get("/family/face-learning", response_class=HTMLResponse)
async def family_face_learning(request: Request):
    """顔学習ページ — 候補顔の一覧とラベル付けUI"""
    if not _is_family_authenticated(request):
        return RedirectResponse("/family/login", status_code=303)
    conn = get_conn()
    try:
        persons = [dict(r) for r in conn.execute(
            "SELECT id, name FROM persons WHERE id > 0 ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "face_learning.html", {
        "persons": persons,
    })


@app.get("/api/pending-notifications")
async def api_pending_notifications(request: Request):
    """未対応の pending_notifications 一覧を返す（家族UIの「未対応リスト」用）。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, notification_type, context_key, message,
                      quick_reply_json, created_at, last_notified_at, notify_count
                 FROM pending_notifications
                WHERE completed_at IS NULL
                ORDER BY created_at DESC
                LIMIT 100"""
        ).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        d = dict(r)
        # quick_reply_json をパースして actions に変換
        try:
            d["actions"] = json.loads(d.get("quick_reply_json") or "[]")
        except Exception:
            d["actions"] = []
        d.pop("quick_reply_json", None)
        items.append(d)
    return {"pending": items}


@app.post("/api/pending-notifications/{notif_id}/respond")
async def api_pending_respond(request: Request, notif_id: int):
    """家族UIから未対応通知に対応する（postback 相当の処理を呼び出す）。

    body: {"data": "<postback data string>"}
    """
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    data = body.get("data", "")
    if not data:
        raise HTTPException(status_code=400, detail="data 必須")

    # 家族UI ユーザーを sender_id として使う（family_ui_admin 等の固定識別子）
    sender_id = "family_ui"

    from ..line_commands import (
        handle_confirm_postback, handle_confirm_dismiss_postback,
        handle_rice_action_postback, handle_bath_classification_postback,
        handle_lock_confirm_postback, handle_session_confirm_postback,
        handle_attribute_postback, handle_merge_postback,
    )
    reply: str | None = None
    try:
        if data.startswith("attribute:"):
            reply = await handle_attribute_postback(data, sender_id)
        elif data.startswith("merge:"):
            reply = await handle_merge_postback(data, sender_id)
        elif data.startswith("confirm:"):
            reply = await handle_confirm_postback(data, sender_id)
        elif data.startswith("confirm_dismiss:"):
            reply = await handle_confirm_dismiss_postback(data, sender_id)
        elif data.startswith("rice_action:"):
            reply = await handle_rice_action_postback(data, sender_id)
        elif data.startswith("bath_cls:"):
            reply = await handle_bath_classification_postback(data, sender_id)
        elif data.startswith("lock_confirm:"):
            reply = await handle_lock_confirm_postback(data, sender_id)
        elif data.startswith("sess_confirm:"):
            reply = await handle_session_confirm_postback(data, sender_id)
        else:
            raise HTTPException(status_code=400, detail=f"不明なアクション: {data[:30]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"処理失敗: {e}")
    return {"ok": True, "reply": reply}


@app.get("/api/learning-stats")
async def api_learning_stats(request: Request):
    """学習データの蓄積状況を返す（家族UIで可視化）。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    conn = get_conn()
    try:
        # 炊飯器分類: 家族手動と自動判定の比率、分類内訳
        rice_rows = conn.execute(
            """SELECT classification, auto_decided, COUNT(*) as cnt
                 FROM rice_classifications
                GROUP BY classification, auto_decided"""
        ).fetchall()
        rice_summary = {
            "manual": {},  # 家族手動分類
            "auto": {},    # システム自動分類
            "total_manual": 0,
            "total_auto": 0,
        }
        for r in rice_rows:
            cls = r["classification"]
            cnt = r["cnt"]
            if r["auto_decided"]:
                rice_summary["auto"][cls] = cnt
                rice_summary["total_auto"] += cnt
            else:
                rice_summary["manual"][cls] = cnt
                rice_summary["total_manual"] += cnt

        # お風呂分類: 確認済み件数
        bath_rows = conn.execute(
            """SELECT confirmed_kind, confirmation_method, COUNT(*) as cnt
                 FROM bath_classifications
                GROUP BY confirmed_kind, confirmation_method"""
        ).fetchall()
        bath_total = sum(r["cnt"] for r in bath_rows)
        bath_confirmed = sum(r["cnt"] for r in bath_rows if r["confirmation_method"] == "line_reply")
        bath_pending = sum(r["cnt"] for r in bath_rows if r["confirmation_method"] is None)

        # セッション分類: 確認済み / 未確認 / 却下
        session_rows = conn.execute(
            """SELECT confirmed, COUNT(*) as cnt FROM meal_sessions GROUP BY confirmed"""
        ).fetchall()
        sess_summary = {
            "confirmed": 0,    # 1: 家族確認済
            "pending": 0,      # 0: 未確認
            "rejected": 0,     # -1: 誤検知
        }
        for r in session_rows:
            c = r["confirmed"]
            if c == 1:
                sess_summary["confirmed"] = r["cnt"]
            elif c == 0:
                sess_summary["pending"] = r["cnt"]
            elif c == -1:
                sess_summary["rejected"] = r["cnt"]

        # 通知応答統計: 詳細内訳（確認済/誤検知/不明/他の家族）
        notif_rows = conn.execute(
            """SELECT notification_type,
                      COUNT(*) as total,
                      SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed,
                      SUM(CASE WHEN completed_action LIKE '%誤検知%' THEN 1 ELSE 0 END) as dismissed,
                      SUM(CASE WHEN completed_action LIKE '%不明%' THEN 1 ELSE 0 END) as unknown_count,
                      SUM(CASE WHEN completed_action LIKE '%他の家族%' THEN 1 ELSE 0 END) as other_family_count,
                      SUM(CASE WHEN completed_at IS NULL THEN 1 ELSE 0 END) as pending
                 FROM pending_notifications
                GROUP BY notification_type
                ORDER BY total DESC"""
        ).fetchall()
        notif_stats = [dict(r) for r in notif_rows]

        # センサー反応回数（直近24h）
        sensor_24h_rows = conn.execute(
            """SELECT source, COUNT(*) as cnt FROM events
                WHERE started_at >= datetime('now', '-24 hours', 'localtime')
                  AND source NOT IN ('camera', 'bathroom_meter')
                GROUP BY source ORDER BY cnt DESC"""
        ).fetchall()
        sensor_24h = [dict(r) for r in sensor_24h_rows]

        # センサー反応回数（直近7日合計）
        sensor_7d_rows = conn.execute(
            """SELECT source, COUNT(*) as cnt FROM events
                WHERE started_at >= datetime('now', '-7 days', 'localtime')
                  AND source NOT IN ('camera', 'bathroom_meter')
                GROUP BY source ORDER BY cnt DESC"""
        ).fetchall()
        sensor_7d = [dict(r) for r in sensor_7d_rows]

        # 候補顔の溜まり数（受動学習のキュー深さ）
        from ..face_id import CANDIDATES_INDEX
        candidate_count = 0
        try:
            if CANDIDATES_INDEX.exists():
                import json as _json
                candidate_count = len(_json.loads(CANDIDATES_INDEX.read_text()))
        except Exception:
            pass
    finally:
        conn.close()

    return {
        "rice": rice_summary,
        "bath": {
            "total": bath_total,
            "confirmed": bath_confirmed,
            "pending": bath_pending,
        },
        "sessions": sess_summary,
        "notifications": notif_stats,
        "sensor_24h": sensor_24h,
        "sensor_7d": sensor_7d,
        "face_candidates_pending": candidate_count,
    }


@app.get("/api/heatmap")
async def api_heatmap(request: Request, days: int = 7):
    """過去N日×24時間のイベント密度を項目ごとに返す（家族UI用）。

    各カテゴリで個別の matrix を返し、フロントエンドはタブ切替で表示する。
    返却形式: {
        "dates": ["04-18 (土)", ...],
        "categories": {
            "meals":  {"icon": "🍴", "name": "食事", "matrix": [[...]], "total": int},
            "baths":  {"icon": "🛁", "name": "お風呂", "matrix": [[...]], "total": int},
            "toilet": {"icon": "🚽", "name": "トイレ", "matrix": [[...]], "total": int},
            "fridge": {"icon": "🧊", "name": "冷蔵庫", "matrix": [[...]], "total": int},
            "motion": {"icon": "🚶", "name": "脱衣所モーション", "matrix": [[...]], "total": int},
        }
    }
    """
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    days = max(1, min(days, 30))
    today = datetime.now().date()
    dates = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    date_idx = {ds: i for i, ds in enumerate(date_strs)}

    def empty_matrix():
        return [[0] * 24 for _ in range(days)]

    # 各カテゴリの定義
    categories = {
        "meals":  {"icon": "🍴", "name": "食事",        "matrix": empty_matrix(), "total": 0},
        "baths":  {"icon": "🛁", "name": "お風呂",      "matrix": empty_matrix(), "total": 0},
        "toilet": {"icon": "🚽", "name": "トイレ",      "matrix": empty_matrix(), "total": 0},
        "fridge": {"icon": "🧊", "name": "冷蔵庫",      "matrix": empty_matrix(), "total": 0},
        "motion": {"icon": "🚶", "name": "脱衣所",      "matrix": empty_matrix(), "total": 0},
    }

    conn = get_conn()
    try:
        ph = ",".join("?" * len(date_strs))

        # 食事 / お風呂: meal_sessions（confirmed=1のみ）
        meal_rows = conn.execute(
            f"""SELECT label, DATE(started_at) as d,
                       CAST(strftime('%H', started_at) AS INTEGER) as h
                  FROM meal_sessions
                 WHERE confirmed = 1 AND DATE(started_at) IN ({ph})""",
            date_strs,
        ).fetchall()
        for r in meal_rows:
            i = date_idx.get(r["d"])
            if i is None or r["h"] is None:
                continue
            label = r["label"] or ""
            if label == "お風呂":
                categories["baths"]["matrix"][i][r["h"]] += 1
                categories["baths"]["total"] += 1
            elif label in ("朝食", "昼食", "夕食", "間食", "おやつ", "夜食") or label.startswith("外食"):
                categories["meals"]["matrix"][i][r["h"]] += 1
                categories["meals"]["total"] += 1

        # トイレ / 冷蔵庫 / 脱衣所モーション: events から集計
        source_to_category = {
            "toilet_door": ("toilet", "open"),
            "fridge": ("fridge", "open"),
            "bath_motion": ("motion", "motion"),
        }
        ev_rows = conn.execute(
            f"""SELECT source, event_type, DATE(started_at) as d,
                       CAST(strftime('%H', started_at) AS INTEGER) as h
                  FROM events
                 WHERE DATE(started_at) IN ({ph})
                   AND source IN ('toilet_door', 'fridge', 'bath_motion')""",
            date_strs,
        ).fetchall()
        for r in ev_rows:
            mapping = source_to_category.get(r["source"])
            if not mapping:
                continue
            cat_key, expected_et = mapping
            if r["event_type"] != expected_et:
                continue
            i = date_idx.get(r["d"])
            if i is None or r["h"] is None:
                continue
            categories[cat_key]["matrix"][i][r["h"]] += 1
            categories[cat_key]["total"] += 1
    finally:
        conn.close()

    return {
        "dates": [d.strftime("%m-%d (%a)") for d in dates],
        "categories": categories,
    }


# ============================================================
# 動的設定（通知ON/OFF・しきい値）
# ============================================================

@app.get("/api/settings")
async def api_list_settings(request: Request):
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    from ..settings import list_settings
    return list_settings()


@app.post("/api/settings")
async def api_set_setting(request: Request):
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    from ..settings import set_setting, SETTING_DEFAULTS
    body = await request.json()
    key = body.get("key", "")
    value = body.get("value", "")
    if key not in SETTING_DEFAULTS:
        raise HTTPException(status_code=400, detail=f"未知の設定キー: {key}")
    set_setting(key, str(value))
    return {"ok": True, "key": key, "value": value}


@app.get("/api/medicine-schedule")
async def api_get_medicine_schedule(request: Request):
    """薬スケジュールを取得。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    return _load_medicine_schedule()


@app.post("/api/medicine-schedule")
async def api_set_medicine_schedule(request: Request):
    """薬スケジュールを登録・更新。body: {"timing": "朝", "hour": 8}"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    timing = body.get("timing", "")
    hour = body.get("hour")
    if timing not in {"朝", "昼", "夜"}:
        raise HTTPException(status_code=400, detail=f"無効なタイミング: {timing}")
    if hour is None or not (0 <= int(hour) <= 23):
        raise HTTPException(status_code=400, detail="時刻は0〜23で指定してください")
    with transaction() as conn:
        conn.execute(
            """INSERT INTO medicine_schedule(timing, hour, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(timing) DO UPDATE SET hour = excluded.hour, updated_at = excluded.updated_at""",
            (timing, int(hour), datetime.now()),
        )
    return {"ok": True, "timing": timing, "hour": int(hour)}


@app.delete("/api/medicine-schedule/{timing}")
async def api_delete_medicine_schedule(request: Request, timing: str):
    """薬スケジュールを削除。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    with transaction() as conn:
        conn.execute("DELETE FROM medicine_schedule WHERE timing = ?", (timing,))
    return {"ok": True}


@app.post("/api/rice-guide")
async def api_set_rice_guide(request: Request):
    """炊飯量を設定。body: {"amount": "2合"}"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    amount = body.get("amount", "").strip()
    if not amount:
        raise HTTPException(status_code=400, detail="量を入力してください")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with transaction() as conn:
        conn.execute("DELETE FROM rice_guide")
        conn.execute(
            "INSERT INTO rice_guide(meal, amount, updated_at) VALUES('next', ?, ?)",
            (amount, now_str),
        )
    return {"ok": True, "amount": amount}


@app.delete("/api/rice-guide")
async def api_clear_rice_guide(request: Request):
    """炊飯量設定をクリア。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    with transaction() as conn:
        conn.execute("DELETE FROM rice_guide")
    return {"ok": True}


@app.post("/api/events/{event_id}/edit")
async def api_edit_event(request: Request, event_id: int):
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    try:
        body = await request.json()
        new_person_id = body.get("person_id")
        new_time = body.get("started_at")  # "HH:MM" or "HH:MM:SS" 形式
        now_str = datetime.now().isoformat()
        with transaction() as conn:
            old = conn.execute("SELECT person_id, started_at FROM events WHERE id = ?", (event_id,)).fetchone()
            if not old:
                raise HTTPException(status_code=404)
            old_pid = old["person_id"]
            old_started = old["started_at"]

            before = {"person_id": old_pid}
            after = {}

            # 人物の変更
            if new_person_id is not None:
                conn.execute(
                    """UPDATE events
                       SET person_id = ?, edited_by = 1, edited_at = ?,
                           original_person_id = COALESCE(original_person_id, ?)
                       WHERE id = ?""",
                    (new_person_id, now_str, old_pid, event_id),
                )
                after["person_id"] = new_person_id

            # 時刻の変更
            if new_time is not None:
                # 既存の日付部分を保持して時刻だけ更新
                if isinstance(old_started, str):
                    date_part = old_started.split("T")[0] if "T" in old_started else old_started.split(" ")[0]
                else:
                    date_part = old_started.strftime("%Y-%m-%d")
                updated_at = f"{date_part}T{new_time}" if len(new_time) == 5 else f"{date_part}T{new_time}"
                conn.execute(
                    "UPDATE events SET started_at = ?, edited_by = 1, edited_at = ? WHERE id = ?",
                    (updated_at, now_str, event_id),
                )
                before["started_at"] = str(old_started)
                after["started_at"] = updated_at

            conn.execute(
                """INSERT INTO edit_log(edited_by, target_table, target_id, before_json, after_json)
                   VALUES(1, 'events', ?, ?, ?)""",
                (event_id, json.dumps(before), json.dumps(after)),
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/events/{event_id}")
async def api_delete_event(request: Request, event_id: int):
    """イベントを削除する（監査ログに記録）。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    try:
        with transaction() as conn:
            old = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not old:
                raise HTTPException(status_code=404)
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            # 関連するmeal_sessionsも削除（family_reportの場合）
            if old["source"] == "family_report":
                conn.execute(
                    "DELETE FROM meal_sessions WHERE person_id = ? AND label = ? AND started_at = ?",
                    (old["person_id"], old["event_type"], old["started_at"]),
                )
            conn.execute(
                """INSERT INTO edit_log(edited_by, target_table, target_id, before_json, after_json)
                   VALUES(1, 'events', ?, ?, ?)""",
                (event_id,
                 json.dumps(dict(old), default=str),
                 json.dumps({"action": "deleted"})),
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 家族用Web UI (認証必須)
# ============================================================

@app.get("/family", response_class=HTMLResponse)
async def family_view(request: Request):
    if not _is_family_authenticated(request):
        return RedirectResponse("/family/login", status_code=303)

    # 日付パラメータ（指定なしなら今日）
    date_param = request.query_params.get("date", "")
    now = datetime.now()
    if date_param:
        selected_date = date_param
    else:
        selected_date = now.strftime("%Y-%m-%d")

    is_today = (selected_date == now.strftime("%Y-%m-%d"))

    # camera と bathroom_meter の通常 reading は件数が多すぎて他のイベントが
    # 埋もれるため家族UIでは非表示（shower_start/abnormal_temp などの特筆イベントは残す）
    raw_events = get_events_by_date(selected_date, 800)
    events = []
    for e in raw_events:
        if e.get("source") == "camera":
            continue
        if e.get("source") == "bathroom_meter" and e.get("event_type") == "reading":
            continue
        events.append(e)
    events = events[:200]

    # サマリ化: 生イベントを「行動」へ集約（複数センサ組合せで「何が起こったか」を予測）
    from ..event_summarizer import summarize_events
    persons_for_summary = {}
    try:
        conn_p = get_conn()
        for r in conn_p.execute("SELECT id, name FROM persons"):
            persons_for_summary[r["id"]] = r["name"]
        conn_p.close()
    except Exception:
        pass
    activities = summarize_events(events, persons_for_summary)

    conn = get_conn()
    try:
        persons = [dict(r) for r in conn.execute(
            "SELECT id, name, role FROM persons ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()
    grandma_sessions = sessions_today(1)
    rice_cooker_state = get_device_state("rice_cooker")
    is_locked = rice_cooker_state["is_locked"] if rice_cooker_state else False

    from ..settings import list_settings
    return templates.TemplateResponse(request, "family.html", {
        "events": events,
        "activities": activities,
        "persons": persons,
        "now": now,
        "grandma_meal_count": len(grandma_sessions),
        "is_locked": is_locked,
        "selected_date": selected_date,
        "is_today": is_today,
        "rice_amount": _load_rice_guide(),
        "medicine_schedule": {m["timing"]: m["hour"] for m in _load_medicine_schedule()},
        "settings": list_settings(),
        "extra_sensors": _build_extra_sensor_status(),
    })


def _build_extra_sensor_status() -> list[dict]:
    """追加センサーの導入状況を返す（家族UI 拡張センサー状態セクション用）。"""
    env_path = BASE.parent.parent / ".env"
    env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    items: list[dict] = []

    # ドライヤー P110M
    dryer_node = int(env.get("HAIR_DRYER_NODE_ID", "0") or "0")
    items.append({
        "icon": "💇",
        "name": "ドライヤー（P110M）",
        "description": "入浴後のドライヤー使用で「髪を洗った」を自動記録",
        "enabled": dryer_node > 0,
        "disabled_reason": "未設定。P110Mペアリング後 .env の HAIR_DRYER_NODE_ID を設定してください。",
    })

    # SwitchBot 防水温湿度計
    sb_enabled = env.get("SWITCHBOT_METER_ENABLED", "0") == "1"
    sb_mac = env.get("SWITCHBOT_METER_MAC", "").strip()
    sb_ok = sb_enabled and bool(sb_mac)
    items.append({
        "icon": "💧",
        "name": "SwitchBot 防水温湿度計",
        "description": "浴室内の湿度急上昇でシャワー使用を直接検知",
        "enabled": sb_ok,
        "disabled_reason": (
            "センサー未購入のため記録不可。" if not sb_mac
            else "MAC設定済みだが有効化されていません（SWITCHBOT_METER_ENABLED=1で有効化）"
        ),
    })

    return items


# ============================================================
# WebSocket (リアルタイムイベント配信)
# ============================================================

@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await websocket.accept()
    q = subscribe()
    try:
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe(q)
