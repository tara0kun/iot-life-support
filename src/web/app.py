"""FastAPIメインアプリ。

2つの経路を提供:
  /tablet  — 祖母用タブレット (読み取り専用、編集UIなし)
  /family  — 家族用 (認証必須、全員閲覧+編集)
  /api     — 内部API (イベント取得、WebSocket)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import datetime, time, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..db import get_conn, init_db, transaction
from ..event_bus import get_events_today, get_recent_events, subscribe, unsubscribe
from ..sessions import sessions_today, last_session
from ..garden import save_daily_score, get_garden_data, FLOWER_TYPES
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

    next_meal = _guess_next_meal(now, sessions)
    minutes_since_last = None
    if last:
        last_time = last["started_at"]
        if isinstance(last_time, str):
            last_time = datetime.fromisoformat(last_time)
        minutes_since_last = int((now - last_time).total_seconds() / 60)

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

    return templates.TemplateResponse(request, "tablet.html", {
        "now": now,
        "sessions": sessions,
        "session_count": len(sessions),
        "last_session": last,
        "minutes_since_last": minutes_since_last,
        "next_meal": next_meal,
        "stamps": stamps,
        "garden": garden,
        "time_greeting": _greeting(now),
        "current_activity": current_activity,
        "alerts": alerts,
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
                    "message": "さっき たべましたよ",
                    "sub": f"{last_meal.get('label')} を {int(minutes_ago)}分まえ に たべました",
                    "color": "#E67E22",
                })

    # 食事3回以上 → さりげなく「よく食べた」
    if meal_count >= 3:
        alerts.append({
            "type": "meal_many",
            "level": "gentle",
            "message": "きょうは よく たべましたね",
            "sub": f"きょう {meal_count}かい たべました",
            "color": "#E67E22",
        })

    # お薬未服用（朝9時以降で未服用）
    if h >= 9 and "お薬" not in done_labels:
        if h < 12:
            alerts.append({
                "type": "medicine",
                "level": "remind",
                "message": "おくすり のみましたか？",
                "sub": "",
                "color": "#EC407A",
            })
        elif h >= 12:
            alerts.append({
                "type": "medicine",
                "level": "warn",
                "message": "おくすり まだですよ",
                "sub": "",
                "color": "#EC407A",
            })

    # お風呂未入浴（17時以降）
    if h >= 17 and "お風呂" not in done_labels:
        alerts.append({
            "type": "bath",
            "level": "remind",
            "message": "おふろ はいりましたか？",
            "sub": "",
            "color": "#29B6F6",
        })

    return alerts


def _greeting(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 10:
        return "おはようございます"
    if 10 <= h < 17:
        return "こんにちは"
    return "こんばんは"


def _guess_next_meal(now: datetime, sessions: list) -> dict | None:
    schedule = [
        ("朝ごはん", time(7, 0)),
        ("お昼ごはん", time(12, 0)),
        ("夕ごはん", time(18, 0)),
    ]
    done_labels = {s.get("label", "") for s in sessions}
    for name, t in schedule:
        meal_dt = datetime.combine(now.date(), t)
        label_map = {"朝ごはん": "朝食", "お昼ごはん": "昼食", "夕ごはん": "夕食"}
        if label_map.get(name) not in done_labels and meal_dt > now:
            minutes = int((meal_dt - now).total_seconds() / 60)
            return {"name": name, "time": t.strftime("%H:%M"), "minutes": minutes}
    return None


def _current_activity(now: datetime, sessions: list) -> str:
    """今の時間帯に応じた活動ガイドを返す。"""
    h = now.hour
    done_labels = {s.get("label", "") for s in sessions}

    if 5 <= h < 7:
        return "あさの じかん 🌅"
    if 7 <= h < 9:
        if "朝食" not in done_labels:
            return "あさごはんの じかん 🍚"
        return "ゆっくり すごす じかん ☕"
    if 9 <= h < 11:
        return "ゆっくり すごす じかん ☕"
    if 11 <= h < 13:
        if "昼食" not in done_labels:
            return "おひるごはんの じかん 🍚"
        return "ゆっくり すごす じかん ☕"
    if 13 <= h < 16:
        return "おひるの じかん ☀️"
    if 16 <= h < 18:
        if "お風呂" not in done_labels:
            return "おふろの じかん 🛁"
        return "ゆうがたの じかん 🌇"
    if 18 <= h < 20:
        if "夕食" not in done_labels:
            return "ゆうごはんの じかん 🍚"
        return "よるの じかん 🌙"
    if 20 <= h < 22:
        return "そろそろ ねる じかん 🌙"
    return "おやすみの じかん 😴"


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

    # 今の時間帯に対応するスタンプを current にする（未完了のもの）
    h = now.hour
    current_label = None
    if 5 <= h < 9:
        current_label = "朝食"
    elif 11 <= h < 13:
        current_label = "昼食"
    elif 16 <= h < 18:
        current_label = "お風呂"
    elif 18 <= h < 20:
        current_label = "夕食"

    if current_label:
        for stamp in all_stamps:
            if stamp["label"] == current_label and not stamp["done"]:
                stamp["current"] = True
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
    events = get_recent_events(100)
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
        "now": datetime.now(),
        "grandma_meal_count": len(grandma_sessions),
        "is_locked": is_locked,
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
