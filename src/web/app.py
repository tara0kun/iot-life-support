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

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..db import get_conn, init_db, transaction
from ..event_bus import get_events_today, get_recent_events, get_events_by_date, subscribe, unsubscribe
from ..sessions import sessions_today, last_session
from ..garden import save_daily_score, get_garden_data, FLOWER_TYPES, _date_to_color
from ..lock_manager import get_device_state, unlock_device

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
    return get_recent_events(limit)


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
    """家族がロックを手動解除する。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    device_name = body.get("device", "rice_cooker")
    reason = body.get("reason", "家族による手動解除")
    node_id = 1  # rice_cooker のMatter node_id

    state = get_device_state(device_name)
    if not state or not state["is_locked"]:
        return {"ok": True, "message": "ロックされていません"}

    success = await unlock_device(device_name, node_id, reason=reason)
    if success:
        # 解除ログを記録
        now = datetime.now()
        with transaction() as conn:
            conn.execute(
                """INSERT INTO events(person_id, source, event_type, started_at, raw_meta)
                   VALUES(NULL, 'family_override', 'unlock', ?, ?)""",
                (now, json.dumps({"device": device_name, "reason": reason}, ensure_ascii=False)),
            )
        return {"ok": True, "message": "解除しました"}
    else:
        raise HTTPException(status_code=500, detail="Matter通信に失敗しました")


# センサー照合マッピング: ボタン→どのセンサーを確認するか
SENSOR_VERIFY = {
    "起床": {"sources": ["camera"], "event_types": ["person_detected"], "window_minutes": 60},
    "お薬": None,  # センサーなし → 常に家族確認
    "お風呂": {"sources": ["bath_door", "bath_motion"], "event_types": ["close", "open", "motion", "bath_end"], "window_minutes": 120},
    "就寝": None,  # センサーなし → 常に家族確認
}


def _verify_sensor(activity: str, person_id: int) -> dict:
    """ボタン押下時にセンサー記録を照合する。"""
    rule = SENSOR_VERIFY.get(activity)
    if rule is None:
        return {"verified": False, "reason": "no_sensor", "message": "センサーがない項目です。家族に確認してもらってください。"}

    window = timedelta(minutes=rule["window_minutes"])
    now = datetime.now()
    since = now - window

    conn = get_conn()
    try:
        placeholders_src = ",".join("?" for _ in rule["sources"])
        placeholders_evt = ",".join("?" for _ in rule["event_types"])
        params = [person_id, since] + rule["sources"] + rule["event_types"]
        row = conn.execute(
            f"""SELECT COUNT(*) as cnt FROM events
                WHERE person_id = ? AND started_at >= ?
                AND source IN ({placeholders_src})
                AND event_type IN ({placeholders_evt})""",
            params,
        ).fetchone()
        if row and row["cnt"] > 0:
            return {"verified": True, "reason": "sensor_confirmed", "message": "センサーで確認できました。"}
        # person_id不問でも探す（未識別の場合）
        params2 = [since] + rule["sources"] + rule["event_types"]
        row2 = conn.execute(
            f"""SELECT COUNT(*) as cnt FROM events
                WHERE started_at >= ?
                AND source IN ({placeholders_src})
                AND event_type IN ({placeholders_evt})""",
            params2,
        ).fetchone()
        if row2 and row2["cnt"] > 0:
            return {"verified": True, "reason": "sensor_confirmed_unidentified", "message": "センサーで確認できました。"}
        return {"verified": False, "reason": "no_sensor_data", "message": "センサーの記録がありません。本当にやりましたか？"}
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

    valid = {"起床", "お薬", "お風呂", "就寝"}
    if activity not in valid:
        raise HTTPException(status_code=400, detail=f"無効な活動: {activity}")

    now = datetime.now()

    # クールダウンチェック
    if not _check_cooldown(activity, person_id):
        return {"ok": False, "verified": False, "reason": "cooldown",
                "message": f"さっき押しましたよ。{TABLET_COOLDOWN_MINUTES}分後にまた押せます。"}

    # センサー照合
    verify = _verify_sensor(activity, person_id)

    # LINE通知（常に送信）
    try:
        from ..notifier import send_line_message
        import asyncio
        if verify["verified"]:
            msg = f"📋 祖母が「{activity}」ボタンを押しました\n✅ センサー確認済み\n時刻: {now.strftime('%H:%M')}"
        elif verify["reason"] == "no_sensor":
            msg = f"📋 祖母が「{activity}」ボタンを押しました\n⚠️ センサーなし（家族確認が必要）\n時刻: {now.strftime('%H:%M')}"
        else:
            msg = f"📋 祖母が「{activity}」ボタンを押しました\n❌ センサー記録なし（確認してください）\n時刻: {now.strftime('%H:%M')}"
        await asyncio.to_thread(send_line_message, msg)
    except Exception:
        pass  # LINE通知失敗は無視

    if verify["verified"]:
        # センサー確認済み → 記録する
        with transaction() as conn:
            conn.execute(
                """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
                   VALUES(?, 'tablet_report', ?, ?, NULL, 1.0, ?)""",
                (person_id, activity, now, json.dumps({"verified": True, "verify_reason": verify["reason"]}, ensure_ascii=False)),
            )
            conn.execute(
                """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
                   VALUES(?, ?, ?, 1, ?)""",
                (person_id, now, now, activity),
            )
        return {"ok": True, "verified": True, "message": f"{activity} を記録しました"}
    else:
        # 未確認 → 記録しない、アラートを返す
        with transaction() as conn:
            conn.execute(
                """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
                   VALUES(?, 'tablet_report', ?, ?, NULL, 0.0, ?)""",
                (person_id, f"{activity}_unverified", now,
                 json.dumps({"verified": False, "verify_reason": verify["reason"]}, ensure_ascii=False)),
            )
        return {"ok": False, "verified": False, "reason": verify["reason"], "message": verify["message"]}


@app.post("/api/quick-record")
async def api_quick_record(request: Request):
    """家族が証人として祖母の行動を記録する。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    activity = body.get("activity", "")
    person_id = body.get("person_id", 1)  # デフォルト: 祖母
    witness = body.get("witness", "家族")

    valid_activities = {"起床", "お薬", "就寝", "お風呂", "トイレ", "おやつ"}
    if activity not in valid_activities:
        raise HTTPException(status_code=400, detail=f"無効な活動: {activity}")

    now = datetime.now()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO events(person_id, source, event_type, started_at, value, confidence, raw_meta)
               VALUES(?, 'family_report', ?, ?, NULL, 1.0, ?)""",
            (person_id, activity, now, json.dumps({"witness": witness}, ensure_ascii=False)),
        )

    # セッション集約に拾われるよう meal_sessions にも直接追加（食事以外の活動）
    if activity in {"起床", "お薬", "就寝", "お風呂", "トイレ"}:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO meal_sessions(person_id, started_at, ended_at, event_count, label)
                   VALUES(?, ?, ?, 1, ?)""",
                (person_id, now, now, activity),
            )

    return {"ok": True, "activity": activity, "time": now.strftime("%H:%M")}


@app.post("/api/family-prompt")
async def api_send_prompt(request: Request):
    """家族から祖母タブレットにメッセージを送る。"""
    if not _is_family_authenticated(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    message = body.get("message", "").strip()
    minutes = body.get("minutes", 60)  # 表示時間（デフォルト60分）
    if not message:
        raise HTTPException(status_code=400, detail="メッセージを入力してください")
    now = datetime.now()
    expires = now + timedelta(minutes=int(minutes))
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_str = expires.strftime("%Y-%m-%d %H:%M:%S")
    with transaction() as conn:
        conn.execute(
            "INSERT INTO family_prompts(message, sent_by, created_at, expires_at) VALUES(?, ?, ?, ?)",
            (message, "家族", now_str, expires_str),
        )
    return {"ok": True, "message": message, "expires_at": expires_str}


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
            """SELECT id, message, sent_by, created_at FROM family_prompts
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
    """許可された送信者ID。未設定ならLINE_USER_IDをフォールバック。"""
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
    if not allowed and line_user_id:
        allowed.add(line_user_id)
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
    events = payload.get("events", [])
    for ev in events:
        if ev.get("type") != "message":
            continue
        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue
        text = msg.get("text", "")
        reply_token = ev.get("replyToken", "")
        sender_id = _extract_sender_id(ev.get("source", {}))

        # 許可リストチェック（allowedが空なら全拒否＝未設定の不正アクセス防止）
        if allowed and sender_id not in allowed:
            _webhook_log.info("未許可送信者: %s", sender_id)
            continue

        if _is_url_request(text):
            reply = _build_url_reply()
            try:
                await asyncio.to_thread(reply_line_message, reply_token, reply)
            except Exception as e:
                _webhook_log.error("LINE返信失敗: %s", e)

    # 常に200を返す（LINEの再送を防ぐ）
    return {"ok": True}


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

    events = get_events_by_date(selected_date, 200)

    conn = get_conn()
    try:
        persons = [dict(r) for r in conn.execute("SELECT id, name, role FROM persons").fetchall()]
    finally:
        conn.close()
    grandma_sessions = sessions_today(1)
    rice_cooker_state = get_device_state("rice_cooker")
    is_locked = rice_cooker_state["is_locked"] if rice_cooker_state else False
    return templates.TemplateResponse(request, "family.html", {
        "events": events,
        "persons": persons,
        "now": now,
        "grandma_meal_count": len(grandma_sessions),
        "is_locked": is_locked,
        "selected_date": selected_date,
        "is_today": is_today,
        "rice_amount": _load_rice_guide(),
        "medicine_schedule": {m["timing"]: m["hour"] for m in _load_medicine_schedule()},
    })


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
