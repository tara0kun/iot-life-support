"""コンポーネント別ヘルスチェック。

各コンポーネントの状態を独立してチェックし、状態変化（OK→NG / NG→OK）時のみLINE通知する。
状態は data/health/*.flag で管理。

cron 例（5分おき）:
  */5 * * * *  cd ~/IoT && venv/bin/python scripts/health_check.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import get_conn
from src.notifier import send_line_message

ROOT = Path(__file__).resolve().parent.parent
FLAG_DIR = ROOT / "data" / "health"
FLAG_DIR.mkdir(parents=True, exist_ok=True)

# 復旧通知の重複防止クールダウン
COOLDOWN_MINUTES = 30


def _flag_path(component: str) -> Path:
    return FLAG_DIR / f"{component}.flag"


def _read_state(component: str) -> tuple[str, datetime] | None:
    """前回の状態 (NG/OK, timestamp) を返す。フラグなしならNone。"""
    p = _flag_path(component)
    if not p.exists():
        return None
    try:
        text = p.read_text().strip()
        state, ts = text.split("|", 1)
        return state, datetime.fromisoformat(ts)
    except Exception:
        return None


def _write_state(component: str, state: str):
    _flag_path(component).write_text(f"{state}|{datetime.now().isoformat()}")


def _notify_change(component: str, ok: bool, detail: str = ""):
    """状態が変化したときのみ通知する。"""
    prev = _read_state(component)
    now = datetime.now()
    new_state = "OK" if ok else "NG"

    if prev is None:
        # 初回: NGなら通知、OKなら静かに記録
        if not ok:
            send_line_message(f"⚠️ {component} が異常です\n{detail}")
        _write_state(component, new_state)
        return

    prev_state, prev_ts = prev
    if prev_state == new_state:
        # 状態同じ → 通知不要、ただし定期再通知（NG継続中、最後の通知から COOLDOWN_MINUTES 以上）
        if not ok and (now - prev_ts) >= timedelta(minutes=COOLDOWN_MINUTES):
            send_line_message(f"⚠️ {component} まだ異常です\n{detail}")
            _write_state(component, "NG")
        return

    # 状態変化 → 通知
    if ok:
        send_line_message(f"✅ {component} が復旧しました")
    else:
        send_line_message(f"⚠️ {component} が異常になりました\n{detail}")
    _write_state(component, new_state)


# ========== 各チェック ==========

def check_web() -> tuple[bool, str]:
    try:
        r = requests.get("http://localhost:8000/tablet", timeout=5, allow_redirects=False)
        if r.status_code in (200, 303, 403):
            return True, ""
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"接続失敗: {type(e).__name__}"


def check_systemd(unit: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        active = r.stdout.strip() == "active"
        return active, "" if active else f"systemctl: {r.stdout.strip()}"
    except Exception as e:
        return False, f"systemctl失敗: {e}"


def check_tunnel() -> tuple[bool, str]:
    """cloudflared プロセス存在 + URLファイルが新鮮か。"""
    try:
        r = subprocess.run(["pgrep", "-f", "cloudflared tunnel"], capture_output=True, text=True, timeout=5)
        if not r.stdout.strip():
            return False, "cloudflared プロセスなし"
    except Exception as e:
        return False, f"pgrep失敗: {e}"

    url_file = ROOT / "data" / "tunnel_url.txt"
    if not url_file.exists():
        return False, "tunnel_url.txt なし"
    if not url_file.read_text().strip().startswith("https://"):
        return False, "URLが無効"
    return True, ""


def check_disk() -> tuple[bool, str]:
    """ディスク残量チェック（10%未満で警告）。"""
    try:
        total, used, free = shutil.disk_usage(ROOT)
        free_pct = free / total * 100
        if free_pct < 10:
            return False, f"残り {free_pct:.1f}% ({free / (1024**3):.1f}GB)"
        return True, ""
    except Exception as e:
        return False, f"disk_usage失敗: {e}"


def check_db() -> tuple[bool, str]:
    """SQLite整合性チェック。"""
    try:
        conn = get_conn()
        try:
            r = conn.execute("PRAGMA quick_check").fetchone()
            ok = r[0] == "ok"
            return ok, "" if ok else f"quick_check: {r[0]}"
        finally:
            conn.close()
    except Exception as e:
        return False, f"DB接続失敗: {type(e).__name__}: {e}"


def check_recent_events() -> tuple[bool, str]:
    """直近イベント（センサー反応）が一定時間内にあるか。日中のみチェック。"""
    now = datetime.now()
    if not (7 <= now.hour < 22):
        return True, ""  # 夜間はスキップ
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                """SELECT MAX(started_at) as latest FROM events
                   WHERE source IN ('rice_cooker', 'camera', 'bath_door',
                                    'bath_motion', 'toilet', 'fridge',
                                    'contact_sensor', 'power_monitor')"""
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["latest"]:
            return True, ""  # データなしはスキップ（初日対応）
        latest = row["latest"]
        if isinstance(latest, str):
            try:
                latest = datetime.fromisoformat(latest.replace("T", " "))
            except ValueError:
                return True, ""
        gap = now - latest
        # 6時間（インシデントレベル）以上で異常判定。anomaly_check.py より緩い
        if gap > timedelta(hours=6):
            return False, f"最終センサー活動 {gap.total_seconds()/3600:.1f}時間前"
        return True, ""
    except Exception:
        return True, ""  # チェック失敗は無音


def main():
    components = [
        ("iot-web", check_web()),
        ("iot-matter", check_systemd("iot-matter")),
        ("iot-monitor", check_systemd("iot-monitor")),
        ("cloudflare-tunnel", check_tunnel()),
        ("disk-space", check_disk()),
        ("database", check_db()),
        ("sensor-activity", check_recent_events()),
    ]

    summary = []
    for name, (ok, detail) in components:
        _notify_change(name, ok, detail)
        mark = "✅" if ok else "❌"
        summary.append(f"{mark} {name}{f' ({detail})' if detail else ''}")

    for line in summary:
        print(line)


if __name__ == "__main__":
    main()
