"""SQLiteスキーマと接続ヘルパ。

設計要件:
- 全家族メンバーの行動を記録 (persons テーブルで管理)
- 家族による編集は祖母UIには見えない → is_edited/edited_by等の列は存在するが
  tablet_view クエリでは参照しない
- 編集履歴は edit_log で別管理 (家族UIのみ)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "iot.db"
DB_PATH.parent.mkdir(exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('grandma', 'family')),
    face_encoding BLOB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES persons(id),  -- NULL = unknown
    source TEXT NOT NULL,                       -- fridge/rice_cooker/ih/camera/manual
    event_type TEXT NOT NULL,                   -- open/close/power_on/power_off/presence
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    value REAL,                                 -- 電力W、滞在秒数等
    confidence REAL DEFAULT 1.0,                -- 顔認識信頼度
    -- 編集メタ (祖母UIには出さない)
    original_person_id INTEGER,
    edited_by INTEGER REFERENCES users(id),
    edited_at TIMESTAMP,
    raw_meta TEXT,                              -- JSON追加情報
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_person_time
    ON events(person_id, started_at);
CREATE INDEX IF NOT EXISTS idx_events_time
    ON events(started_at);

CREATE TABLE IF NOT EXISTS meal_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons(id),
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP NOT NULL,
    event_count INTEGER DEFAULT 0,
    label TEXT                                  -- 朝食/昼食/夕食/間食 自動推定
);

CREATE INDEX IF NOT EXISTS idx_sessions_person_time
    ON meal_sessions(person_id, started_at);

CREATE TABLE IF NOT EXISTS session_events (
    session_id INTEGER NOT NULL REFERENCES meal_sessions(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY (session_id, event_id)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    person_id INTEGER REFERENCES persons(id),
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'viewer')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS edit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edited_by INTEGER NOT NULL REFERENCES users(id),
    target_table TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    before_json TEXT,
    after_json TEXT,
    edited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS device_state (
    -- P115等のプラグロック状態管理 (Phase 2)
    device_name TEXT PRIMARY KEY,
    is_locked INTEGER DEFAULT 0,
    last_cycle_at TIMESTAMP,
    cycle_count_today INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS family_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    sent_by TEXT DEFAULT '家族',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,              -- 表示期限
    dismissed INTEGER NOT NULL DEFAULT 0        -- 祖母が確認済みか
);

CREATE TABLE IF NOT EXISTS medicine_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timing TEXT NOT NULL UNIQUE,                -- 朝/昼/夜
    hour INTEGER NOT NULL,                      -- リマインド開始時刻（時）
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rice_guide (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal TEXT NOT NULL UNIQUE,              -- 朝食/昼食/夕食
    amount TEXT NOT NULL,                   -- 例: "1合", "2合"
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons(id),
    date TEXT NOT NULL,                         -- YYYY-MM-DD
    done_count INTEGER NOT NULL DEFAULT 0,      -- 達成したスタンプ数
    total_count INTEGER NOT NULL DEFAULT 7,     -- 全スタンプ数
    flower_type TEXT NOT NULL DEFAULT 'seed',   -- seed/sprout/bud/flower/bloom/big/bouquet
    details TEXT,                               -- JSON: どのスタンプを達成したか
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, date)
);

CREATE TABLE IF NOT EXISTS care_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL UNIQUE,             -- 例: 朝のお薬確認、夜の様子見
    assignee_name TEXT,                         -- 例: 母、孫（NULLなら未割当）
    reminder_hour INTEGER,                      -- リマインダー送信時刻 (0-23)
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS care_task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES care_tasks(id),
    date TEXT NOT NULL,                         -- YYYY-MM-DD
    done_by TEXT,                               -- 誰が対応したか (LINE sender ID or 名前)
    done_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    note TEXT,
    UNIQUE(task_id, date)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pending_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_type TEXT NOT NULL,            -- 'attribute_session', 'anomaly_*' 等
    context_key TEXT NOT NULL,                  -- 'session_42' 等 (typeとセットで一意)
    message TEXT NOT NULL,
    quick_reply_json TEXT,                      -- Quick Replyボタン定義（再通知用）
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_notified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notify_count INTEGER NOT NULL DEFAULT 1,
    completed_at TIMESTAMP,
    completed_by TEXT,                          -- LINE sender_id
    completed_action TEXT                       -- 結果説明（"祖母として記録" 等）
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_notif_context
    ON pending_notifications(notification_type, context_key);
CREATE INDEX IF NOT EXISTS idx_pending_notif_pending
    ON pending_notifications(completed_at, last_notified_at);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _seed_default_persons(conn)
    finally:
        conn.close()


UNASSIGNED_PERSON_ID = 0


def _seed_default_persons(conn: sqlite3.Connection) -> None:
    # id=0 を「未確定」用のセンチネル行として確保（role='family' は CHECK 制約適合のため）
    conn.execute(
        "INSERT OR IGNORE INTO persons(id, name, role) VALUES(0, '未確定', 'family')"
    )
    existing = conn.execute("SELECT COUNT(*) FROM persons WHERE id > 0").fetchone()[0]
    if existing == 0:
        conn.executemany(
            "INSERT INTO persons(name, role) VALUES(?, ?)",
            [
                ("祖母", "grandma"),
                ("母", "family"),
                ("祖父", "family"),
            ],
        )
        conn.commit()
    # 家族UIの編集操作用デフォルトユーザー
    existing_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing_users == 0:
        from hashlib import sha256
        conn.execute(
            "INSERT INTO users(name, person_id, password_hash, role) VALUES(?, ?, ?, ?)",
            ("admin", None, sha256(b"changeme").hexdigest(), "admin"),
        )
        conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
