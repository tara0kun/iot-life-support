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
    """Tailscale Funnel が稼働中か (6/3〜 cloudflared から移行)。"""
    try:
        r = subprocess.run(
            ["tailscale", "funnel", "status"],
            capture_output=True, text=True, timeout=5,
        )
        # 'Funnel on' を含めば稼働中
        if "Funnel on" not in r.stdout:
            return False, "Tailscale Funnel が無効"
    except Exception as e:
        return False, f"tailscale funnel status 失敗: {e}"

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
    """各センサーが個別に生きているか。日中のみチェック。

    **1 つでも反応していれば OK、にしてはいけない。** 以前は 10 種類を
    まとめて MAX で見ていたため、カメラが動いている限り残り 9 種が何日
    死んでいても ✅ を返した。実際 2026-09-04〜09-15 の 11 日間、ドアと
    モーションが全滅したまま「✅ sensor-activity」を出し続けていた
    (原因は TP-Link 機器の TPAP 暗号化に python-kasa が未対応)。

    センサーごとに性質が違うので、しきい値も分ける。

        常時反応する      camera / bathroom_meter        -> 2 時間
        使ったときだけ    fridge / bath_door / toilet_door -> 30 時間
        1 日 1〜2 回      rice_cooker / rice_cooker_lid  -> 40 時間

    家族の操作(family_report など)は「無くて当たり前」なので見ない。

    **例外を ✅ にしない。** DB が読めない状態を「正常」と報告すると、
    壊れていることに気づけない。
    """
    now = datetime.now()
    if not (7 <= now.hour < 22):
        return True, ""  # 夜間はスキップ

    # (source, 許容時間, 表示名)
    watched = [
        ("camera", 2, "カメラ"),
        ("bathroom_meter", 2, "浴室温湿度"),
        ("fridge", 30, "冷蔵庫"),
        ("bath_door", 30, "浴室ドア"),
        ("bath_motion", 30, "脱衣所モーション"),
        ("toilet_door", 30, "トイレドア"),
        ("rice_cooker_lid", 40, "炊飯器の蓋"),
        ("rice_cooker", 40, "炊飯器の電力"),
    ]
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT source, MAX(started_at) AS latest FROM events "
                "GROUP BY source"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        # **黙って ✅ にしない。** 読めないこと自体が異常である
        return False, f"DB を読めない ({type(exc).__name__})"

    latest_by_source = {}
    for row in rows:
        value = row["latest"]
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("T", " "))
            except ValueError:
                continue
        if value is not None:
            latest_by_source[row["source"]] = value

    if not latest_by_source:
        return True, ""  # データなしはスキップ（初日対応）

    stale = []
    for source, limit_hours, label in watched:
        latest = latest_by_source.get(source)
        if latest is None:
            continue  # 一度も記録が無いものは対象外（未設置など）
        hours = (now - latest).total_seconds() / 3600
        if hours > limit_hours:
            stale.append(f"{label} {hours:.0f}時間")

    if stale:
        return False, "反応なし: " + " / ".join(stale)
    return True, ""


def main():
    components = [
        ("iot-web", check_web()),
        ("iot-matter", check_systemd("iot-matter")),
        ("iot-monitor", check_systemd("iot-monitor")),
        ("tailscale-funnel", check_tunnel()),
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
