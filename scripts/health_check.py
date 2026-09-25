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
COOLDOWN_MINUTES = 30  # (後方互換のため残置。実際の間隔は BACKOFF_MINUTES)

# NG が続くときの再通知間隔。回を追うごとに伸ばし、使い切ったら打ち切る。
BACKOFF_MINUTES = [30, 60, 240, 1440]
MAX_NG_NOTIFICATIONS = len(BACKOFF_MINUTES) + 1  # 初回 + 再通知4回 = 最大5通


def _flag_path(component: str) -> Path:
    return FLAG_DIR / f"{component}.flag"


def _read_state(component: str) -> tuple[str, datetime, int, str] | None:
    """前回の状態 (NG/OK, timestamp) を返す。フラグなしならNone。"""
    p = _flag_path(component)
    if not p.exists():
        return None
    try:
        text = p.read_text().strip()
        parts = text.split("|")
        state, ts = parts[0], parts[1]
        # 旧形式 (state|ts) のフラグは「1回通知済み」とみなす
        count = int(parts[2]) if len(parts) > 2 else 1
        key = parts[3] if len(parts) > 3 else ""
        return state, datetime.fromisoformat(ts), count, key
    except Exception:
        return None


def _write_state(component: str, state: str, count: int = 0, key: str = ""):
    _flag_path(component).write_text(
        f"{state}|{datetime.now().isoformat()}|{count}|{key}"
    )


def _notify_change(component: str, ok: bool | None, detail: str = "", key: str = ""):
    """状態が変化したときのみ通知する。

    ok=None は「判定不能」（例: 夜間でセンサー確認をスキップ）。このときは
    状態ファイルも書き換えず、通知もしない。正常と誤認させないための第3状態。

    NG が続く間は BACKOFF_MINUTES に従って間隔を伸ばしながら再通知し、
    MAX_NG_NOTIFICATIONS 回で打ち切る。以前は30分固定・上限なしで、
    人手が要る故障（電池切れ等）では一日中鳴り続けていた。
    """
    if ok is None:
        return

    prev = _read_state(component)
    now = datetime.now()
    new_state = "OK" if ok else "NG"

    if prev is None:
        # 初回: NGなら通知、OKなら静かに記録
        if not ok:
            send_line_message(f"⚠️ {component} が異常です\n{detail}")
            _write_state(component, "NG", 1, key)
        else:
            _write_state(component, "OK", 0, key)
        return

    prev_state, prev_ts, count, prev_key = prev
    if prev_state == new_state:
        if ok:
            return  # 正常が続いている → 何もしない

        # **中身が変わったら打ち切りを解除する。**
        # sensor-activity は8センサーを1コンポーネントに束ねているので、
        # 「蓋センサーが死んでいる」で通知予算を使い切ったあとにカメラや
        # 浴室温湿度が死んでも、状態は NG のままで通知が出なかった。
        # 7eba82c が「合算で見て11日間見逃した」のと同じ失敗が、しきい値層
        # ではなく通知層で復活していた。故障センサーの顔ぶれ (key) が
        # 変わったら新しい障害とみなして数え直す。
        if key != prev_key:
            log_line = f"⚠️ {component} の異常内容が変わりました\n{detail}"
            send_line_message(log_line)
            _write_state(component, "NG", 1, key)
            return

        if count >= MAX_NG_NOTIFICATIONS:
            return  # 打ち切り済み。復旧か、内容が変わった時だけ再び喋る
        idx = min(max(count - 1, 0), len(BACKOFF_MINUTES) - 1)
        if (now - prev_ts) < timedelta(minutes=BACKOFF_MINUTES[idx]):
            return
        count += 1
        tail = ""
        if count >= MAX_NG_NOTIFICATIONS:
            tail = "\n\n（繰り返しはこれで最後にします。直ったらお知らせします）"
        send_line_message(f"⚠️ {component} まだ異常です\n{detail}{tail}")
        _write_state(component, "NG", count, key)
        return

    # 状態変化 → 通知
    if ok:
        send_line_message(f"✅ {component} が復旧しました")
        _write_state(component, "OK", 0, key)
    else:
        send_line_message(f"⚠️ {component} が異常になりました\n{detail}")
        _write_state(component, "NG", 1, key)


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


def check_recent_events() -> tuple[bool | None, str, str]:
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
        # **夜間は「正常」ではなく「判定不能」**。以前はここで (True, "") を返して
        # いたため、_notify_change が NG→OK の復旧とみなし、センサーが壊れたまま
        # 毎晩22時に「✅ sensor-activity が復旧しました」という嘘を送っていた。
        # None は「状態を更新も通知もしない」を意味する。
        return None, "", ""

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
        # **判定不能であって正常ではない。** events が1件も読めないのは
        # DB 破損・復元直後・クリーンアップ事故のいずれかで、ここで True を
        # 返すと「✅ 復旧しました」の嘘が出る（夜間分岐と同じ失敗）。
        return None, "", ""

    stale = []
    stale_sources = []
    evaluated = 0
    for source, limit_hours, label in watched:
        latest = latest_by_source.get(source)
        if latest is None:
            continue  # 一度も記録が無いものは対象外（未設置など）
        evaluated += 1
        hours = (now - latest).total_seconds() / 3600
        if hours > limit_hours:
            stale.append(f"{label} {hours:.0f}時間")
            stale_sources.append(source)

    if evaluated == 0:
        # 監視対象の source が1つも評価できなかった＝判定材料ゼロ。
        # （events に family_report 等だけが残っている状態など）
        return None, "", ""

    if stale:
        # 第3要素は「どのセンサーが死んでいるか」の識別キー。経過時間は
        # 含めない（毎時変わってしまうため）。_notify_change はこのキーが
        # 変わったら打ち切り済みでも再通知を再開する。
        return False, "反応なし: " + " / ".join(stale), ",".join(sorted(stale_sources))
    return True, "", ""


def check_heartbeat_config() -> tuple[bool, str]:
    """外部死活監視 (healthchecks.io) が設定済みか。

    **Pi 自身が止まったら、Pi の上で動く監視は全部一緒に止まる。**
    それを検知できる唯一の仕組みが scripts/heartbeat.sh の外形監視だが、
    .env の HEARTBEAT_URL が空だと heartbeat.sh:14-16 が即 exit 0 して
    何も起きない。2026-06-25〜07-01 の6日間ダウンを受けて仕組みは追加された
    のに、設定1行が空のまま3ヶ月気づかれなかった。ここで見張って再発を防ぐ。

    設定手順は HANDOFF.md 「外部死活監視」節。
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return False, ".env が無い"
    for line in env_path.read_text().splitlines():
        if line.startswith("HEARTBEAT_URL="):
            url = line.split("=", 1)[1].strip()
            if url:
                return True, ""
            break
    return False, (
        "外部死活監視が未設定 (.env の HEARTBEAT_URL が空)。"
        "Pi が丸ごと止まると誰も気づけません。"
        "設定手順: HANDOFF.md 「外部死活監視」節"
    )


def main():
    components = [
        ("iot-web", check_web()),
        ("iot-matter", check_systemd("iot-matter")),
        ("iot-monitor", check_systemd("iot-monitor")),
        ("tailscale-funnel", check_tunnel()),
        ("disk-space", check_disk()),
        ("database", check_db()),
        ("sensor-activity", check_recent_events()),
        ("heartbeat-config", check_heartbeat_config()),
    ]

    summary = []
    for name, res in components:
        # 2要素 (ok, detail) と 3要素 (ok, detail, key) の両方を許す
        ok, detail = res[0], res[1]
        key = res[2] if len(res) > 2 else ""
        _notify_change(name, ok, detail, key)
        mark = "⏸" if ok is None else ("✅" if ok else "❌")
        summary.append(f"{mark} {name}{f' ({detail})' if detail else ''}")

    for line in summary:
        print(line)


if __name__ == "__main__":
    main()
