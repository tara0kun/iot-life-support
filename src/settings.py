"""動的設定ヘルパ（家族UIから編集できる運用パラメータ）。

settings テーブルに key-value 形式で保存。
通知ON/OFF・しきい値などはここから参照する。
"""
from __future__ import annotations

from typing import Any

from .db import get_conn, transaction


# 既知設定キーのデフォルトと説明
SETTING_DEFAULTS: dict[str, tuple[str, str]] = {
    # 通知ON/OFF（"1"=ON, "0"=OFF）
    "notify_medicine_enabled": ("1", "お薬リマインダー通知"),
    "notify_bath_enabled": ("1", "お風呂リマインダー通知"),
    "notify_summary_enabled": ("1", "1日のまとめ通知"),
    "notify_care_tasks_enabled": ("1", "家族タスクのリマインダー通知"),
    "notify_anomaly_enabled": ("1", "異常検知の通知（深夜炊飯器・無反応・冷蔵庫）"),
    "notify_weekly_report_enabled": ("1", "週次レポート通知"),
    "notify_url_change_enabled": ("1", "Cloudflare Tunnel URL変更通知"),

    # 異常検知しきい値
    "anomaly_inactivity_hours": ("4", "センサー無反応で安否確認する時間（時間）"),
    "anomaly_night_rice_start_hour": ("2", "深夜炊飯器検知の開始時刻（時）"),
    "anomaly_night_rice_end_hour": ("5", "深夜炊飯器検知の終了時刻（時）"),
    "anomaly_fridge_open_minutes": ("30", "冷蔵庫開きっぱなし検知の分数"),

    # 食事ロック
    "meal_lock_window_minutes": ("90", "ロック発動: 直近食事から何分以内なら再食事行動でロックするか"),
    "meal_count_warn_threshold": ("3", "「食べ過ぎ」警告の食事回数しきい値"),
}


def get_setting(key: str, default: Any = None) -> str:
    """設定値を取得。DBになければデフォルト→引数の順でフォールバック。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row:
            return row["value"]
    finally:
        conn.close()
    if key in SETTING_DEFAULTS:
        return SETTING_DEFAULTS[key][0]
    return default


def get_bool(key: str, default: bool = True) -> bool:
    v = get_setting(key, "1" if default else "0")
    return v == "1"


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get_setting(key, str(default)))
    except (ValueError, TypeError):
        return default


def set_setting(key: str, value: str):
    desc = SETTING_DEFAULTS.get(key, (None, ""))[1]
    with transaction() as conn:
        conn.execute(
            """INSERT INTO settings(key, value, description) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = CURRENT_TIMESTAMP""",
            (key, str(value), desc),
        )


def list_settings() -> list[dict]:
    """既知の全設定を、現在値とデフォルト・説明込みで返す（家族UI用）。"""
    conn = get_conn()
    try:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()}
    finally:
        conn.close()
    out = []
    for key, (default, desc) in SETTING_DEFAULTS.items():
        out.append({
            "key": key,
            "value": rows.get(key, default),
            "default": default,
            "description": desc,
            "is_bool": key.startswith("notify_") and key.endswith("_enabled"),
        })
    return out
