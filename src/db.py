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
    label TEXT,                                 -- 朝食/昼食/夕食/間食 自動推定
    confirmed INTEGER DEFAULT 0,                -- 0=未確定（LINE確認待ち）, 1=家族確認済, -1=誤検知として却下
    confirmed_by TEXT,                          -- LINE sender_id
    confirmed_at TIMESTAMP
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
    dismissed INTEGER NOT NULL DEFAULT 0,       -- 祖母が確認済みか
    priority TEXT NOT NULL DEFAULT 'normal'     -- 'critical'(音声強調)/'normal'/'silent'(表示のみ)
        CHECK(priority IN ('critical', 'normal', 'silent'))
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

CREATE TABLE IF NOT EXISTS meal_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES meal_sessions(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES persons(id),
    file_name TEXT NOT NULL,                       -- data/meal_photos/ 配下のファイル名
    file_size INTEGER,
    width INTEGER,
    height INTEGER,
    taken_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP                           -- 家族が削除したら設定
);
CREATE INDEX IF NOT EXISTS idx_meal_photos_taken
    ON meal_photos(taken_at);
CREATE INDEX IF NOT EXISTS idx_meal_photos_session
    ON meal_photos(session_id);

CREATE TABLE IF NOT EXISTS rice_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,                              -- 関連eventsレコードID（削除済みでもOK）
    power_w REAL NOT NULL,                         -- 検知時の電力
    hour_of_day INTEGER NOT NULL,                  -- 0-23
    lid_recently_opened INTEGER DEFAULT 0,         -- 蓋開30秒以内なら1
    classification TEXT NOT NULL                   -- cook/keep_warm/lid_only/lid_meal/unknown
        CHECK(classification IN ('cook', 'keep_warm', 'lid_only', 'lid_meal', 'unknown')),
    classified_by TEXT,                            -- LINE sender_id
    classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    auto_decided INTEGER DEFAULT 0                 -- 0=家族手動 1=システム自動判定
);
CREATE INDEX IF NOT EXISTS idx_rice_cls_features
    ON rice_classifications(power_w, hour_of_day, lid_recently_opened);

CREATE TABLE IF NOT EXISTS family_line_users (
    line_user_id TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id),
    display_name TEXT,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP
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

CREATE TABLE IF NOT EXISTS bath_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    hour_of_day INTEGER NOT NULL,                  -- 0-23
    -- 検知時の信号スナップショット
    door_was_closed INTEGER DEFAULT 0,             -- ドアが閉じていたか（祖母推定の手掛かり）
    humidity_baseline REAL,                        -- 検知前5分の平均湿度
    humidity_peak REAL,                            -- 検知中の最大湿度
    humidity_delta REAL,                           -- peak - baseline
    temperature_delta REAL,                        -- 温度変化量
    motion_count INTEGER DEFAULT 0,                -- 脱衣所モーション回数
    active_person_id INTEGER,                      -- 検知時の active_person（カメラ識別）
    -- 確定結果（LINE回答 or 自動判定）
    confirmed_person_id INTEGER,                   -- NULL=未回答、0=誰もいない（湯はり/清掃）、>0=人物ID
    confirmed_kind TEXT,                           -- 'bathing'/'yu_filling'/'cleaning'/'unknown'
    confirmation_method TEXT,                      -- 'line_reply'/'auto_active_person'/'auto_learned'
    confirmed_by TEXT,                             -- 回答した LINE user_id
    confirmed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bath_cls_pending
    ON bath_classifications(confirmed_person_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_bath_cls_features
    ON bath_classifications(hour_of_day, door_was_closed, active_person_id);
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
        _migrate_rice_classifications_check(conn)
        _migrate_meal_sessions_confirmed(conn)
    finally:
        conn.close()


def _migrate_meal_sessions_confirmed(conn: sqlite3.Connection) -> None:
    """meal_sessions に confirmed/confirmed_by/confirmed_at 列を追加。

    既存セッション（移行前）は confirmed=1 とみなして既存表示を維持する。
    """
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(meal_sessions)").fetchall()]
        if "confirmed" not in cols:
            conn.executescript("""
                ALTER TABLE meal_sessions ADD COLUMN confirmed INTEGER DEFAULT 0;
                ALTER TABLE meal_sessions ADD COLUMN confirmed_by TEXT;
                ALTER TABLE meal_sessions ADD COLUMN confirmed_at TIMESTAMP;
                UPDATE meal_sessions SET confirmed = 1 WHERE confirmed IS NULL OR confirmed = 0;
            """)
            conn.commit()
            __import__('logging').getLogger("db").info(
                "meal_sessions に confirmed カラムを追加（既存は確定済として移行）"
            )
    except Exception as e:
        __import__('logging').getLogger("db").warning(
            "meal_sessions マイグレーション失敗: %s", e
        )


def _migrate_rice_classifications_check(conn: sqlite3.Connection) -> None:
    """rice_classifications.classification の CHECK 制約を 'lid_meal' 含むものに更新。

    SQLite は CHECK 制約を ALTER できないため、既存テーブルが古いCHECK
    （cook/keep_warm/lid_only/unknown のみ）を持っている場合はテーブル再作成で更新する。
    データは保持される。
    """
    try:
        # 現テーブルのスキーマDDLを取得
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='rice_classifications'"
        ).fetchone()
        if not row or "lid_meal" in (row[0] or ""):
            return  # 既に新CHECK含む or テーブル無し
        # 旧CHECKを含む → 再作成して移行
        conn.executescript("""
            ALTER TABLE rice_classifications RENAME TO rice_classifications_old;
            CREATE TABLE rice_classifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER,
                power_w REAL NOT NULL,
                hour_of_day INTEGER NOT NULL,
                lid_recently_opened INTEGER DEFAULT 0,
                classification TEXT NOT NULL
                    CHECK(classification IN ('cook', 'keep_warm', 'lid_only', 'lid_meal', 'unknown')),
                classified_by TEXT,
                classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                auto_decided INTEGER DEFAULT 0
            );
            INSERT INTO rice_classifications
                SELECT * FROM rice_classifications_old;
            DROP TABLE rice_classifications_old;
            CREATE INDEX IF NOT EXISTS idx_rice_cls_features
                ON rice_classifications(power_w, hour_of_day, lid_recently_opened);
        """)
        conn.commit()
        log = __import__('logging').getLogger("db")
        log.info("rice_classifications テーブルをマイグレーションしました（lid_meal 追加）")
    except Exception as e:
        __import__('logging').getLogger("db").warning(
            "rice_classifications マイグレーション失敗: %s", e
        )


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
