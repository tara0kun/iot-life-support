"""機器のロック/アンロック管理。

Phase 1 MVP:
- 食事セッション検知後、P110M経由で炊飯器/IHの電源をOFFロック
- 一定時間経過 or 家族バイパスでアンロック
- 祖母がアンロック要求 → 直近食事チェック → 警告表示

ロック状態はDBのdevice_stateテーブルで管理。
実際のON/OFF制御はMatter WebSocket経由。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp

from .db import get_conn, transaction
from .sessions import last_meal_session

log = logging.getLogger("lock_manager")

MATTER_WS = "ws://localhost:5580/ws"

LOCK_DURATION_MINUTES = 120
RECENT_MEAL_MINUTES = 90


async def _matter_set_power(node_id: int, on: bool) -> bool:
    """Matter経由でP110Mのコンセント通電をON/OFFする。

    matter-server 8.x の API（APICommand.DEVICE_COMMAND = "device_command"）に
    対応。Matter OnOff cluster (id=6) の On/Off コマンドを送信する。
    """
    try:
        async with aiohttp.ClientSession() as session:
            ws = await session.ws_connect(MATTER_WS)
            # サーバ hello を1回受信
            await asyncio.wait_for(ws.receive_json(), timeout=5)
            cmd_name = "On" if on else "Off"  # Matter Cluster Command クラス名
            await ws.send_json({
                "message_id": "lock",
                "command": "device_command",
                "args": {
                    "node_id": node_id,
                    "endpoint_id": 1,
                    "cluster_id": 6,           # OnOff cluster
                    "command_name": cmd_name,
                    "payload": {},
                },
            })
            # message_id 一致するレスポンスまで読む（途中のサブスクリプション通知をスキップ）
            for _ in range(10):
                resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
                if resp.get("message_id") == "lock":
                    await ws.close()
                    if resp.get("error_code") is not None:
                        log.warning("Matter制御エラー: %s", resp.get("details"))
                        return False
                    return True
            await ws.close()
            log.warning("Matter制御: lock応答が見つからず")
            return False
    except Exception as e:
        log.error("Matter制御失敗: %s", e)
        return False


def get_device_state(device_name: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM device_state WHERE device_name = ?", (device_name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _ensure_device(device_name: str) -> None:
    with transaction() as conn:
        exists = conn.execute(
            "SELECT 1 FROM device_state WHERE device_name = ?", (device_name,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO device_state(device_name) VALUES(?)", (device_name,)
            )


async def lock_device(device_name: str, node_id: int, reason: str = "") -> bool:
    _ensure_device(device_name)
    success = await _matter_set_power(node_id, on=False)
    if success:
        with transaction() as conn:
            conn.execute(
                """UPDATE device_state
                   SET is_locked = 1, last_cycle_at = ?, updated_at = ?,
                       cycle_count_today = cycle_count_today + 1,
                       unlock_due_at = ?
                   WHERE device_name = ?""",
                (datetime.now(), datetime.now(),
                 datetime.now() + timedelta(minutes=LOCK_DURATION_MINUTES),
                 device_name),
            )
        log.info("[%s] ロック実行 (理由: %s)", device_name, reason)
    return success


async def unlock_device(device_name: str, node_id: int, reason: str = "") -> bool:
    success = await _matter_set_power(node_id, on=True)
    if success:
        with transaction() as conn:
            conn.execute(
                """UPDATE device_state
                   SET is_locked = 0, updated_at = ?, unlock_due_at = NULL
                   WHERE device_name = ?""",
                (datetime.now(), device_name),
            )
        log.info("[%s] アンロック (理由: %s)", device_name, reason)
    return success


def should_warn_recent_meal(person_id: int) -> dict | None:
    """直近の食事があれば警告情報を返す。なければNone。

    炊飯器ロック確認の起点。お風呂や家族の手動スタンプ（起床/お薬/就寝）を
    「直近の食事」と数えないよう、食事ラベルのセッションだけを見る。
    """
    last = last_meal_session(person_id)
    if not last:
        return None
    last_time = last["started_at"]
    if isinstance(last_time, str):
        last_time = datetime.fromisoformat(last_time)
    minutes_ago = (datetime.now() - last_time).total_seconds() / 60
    if minutes_ago < RECENT_MEAL_MINUTES:
        return {
            "last_meal_label": last.get("label", "食事"),
            "last_meal_time": last_time.strftime("%H:%M"),
            "minutes_ago": int(minutes_ago),
        }
    return None


# 機器名 → Matter node_id。従来は呼び出し側4箇所に 1 が直書きされていた。
DEVICE_NODE_IDS = {"rice_cooker": 1}


async def release_expired_locks() -> list[str]:
    """期限 (unlock_due_at) を過ぎたロックを解除する。戻り値=解除した機器名。

    **cron から叩くこと。** 自動解除を in-process の asyncio.sleep に持たせると、
    iot-monitor の再起動や停電で解除予約が消えて永久ロックになる。
    DB に期限を持たせて外から回収すれば、プロセスの生死に依存しない。
    """
    released: list[str] = []

    # is_locked=1 なのに期限が無い＝この機能より前にロックされたか、
    # 何らかの経路で期限が落ちた状態。放置すると永久ロックになる（2026-08-30 の
    # 50時間ロックがまさにこれ）。last_cycle_at を起点に期限を補完して回収する。
    with transaction() as conn:
        conn.execute(
            """UPDATE device_state
                  SET unlock_due_at = datetime(
                          COALESCE(last_cycle_at, updated_at, CURRENT_TIMESTAMP),
                          ?
                      )
                WHERE is_locked = 1 AND unlock_due_at IS NULL""",
            (f"+{LOCK_DURATION_MINUTES} minutes",),
        )

    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT device_name, unlock_due_at FROM device_state
                WHERE is_locked = 1 AND unlock_due_at IS NOT NULL
                  AND unlock_due_at <= ?""",
            (datetime.now(),),
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        name = r["device_name"]
        node_id = DEVICE_NODE_IDS.get(name)
        if node_id is None:
            log.warning("[%s] node_id 不明のため自動解除できない", name)
            continue
        ok = await unlock_device(name, node_id, reason="ロック期限切れによる自動解除")
        if ok:
            released.append(name)
        else:
            log.warning("[%s] 期限切れ解除に失敗（次回の cron で再試行）", name)
    return released


async def auto_lock_after_meal(device_name: str, node_id: int) -> None:
    """食事セッション完了後に呼ばれる。自動ロック→一定時間後に自動アンロック。"""
    await lock_device(device_name, node_id, reason="食事後自動ロック")
    await asyncio.sleep(LOCK_DURATION_MINUTES * 60)
    state = get_device_state(device_name)
    if state and state["is_locked"]:
        await unlock_device(device_name, node_id, reason="時間経過による自動アンロック")
