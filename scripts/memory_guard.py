"""iot-monitor が膨らみすぎたら再起動して、Pi 全体が落ちるのを防ぐ。

**なぜ cgroup ではなくこれなのか。**
この Pi（Raspberry Pi 5）はファームウェアが `cgroup_disable=memory` を
注入しており、memory コントローラが無効になっている。

    /proc/cmdline                     → cgroup_disable=memory
    /sys/fs/cgroup/cgroup.controllers → cpuset cpu io pids（memory が無い）

そのため unit の MemoryMax / MemoryHigh は **設定値として見えるだけで強制されない**。
`systemctl show` は値を返すので効いているように見えるが、MemoryCurrent は
`[not set]` になる。有効化には cmdline.txt の書き換えと再起動が必要で、
壊すと起動しなくなる（実機は祖母宅にあり、復旧に現地対応が必要）。

実際に起きたこと（2026-10-23）:

    14:32:41 kernel: matter-server invoked oom-killer
    14:32:42 kernel: oom-kill: global_oom, task=python, pid=149777
    14:32:42 kernel: Out of memory: Killed process (python) anon-rss:1344288kB
    14:38:57 Pi が再起動

殺された python（iot-monitor）は 1344MB で、設定した上限 2GB を下回っていた。
システム全体が先に枯れるので、上限 2GB では間に合わない。

**これは対症療法である。** 本来は iot-monitor がなぜ 1GB を超えて育つのか
（dlib / face_recognition / OpenCV のどこで溜まるのか）を直すべき。
ここでやるのは「Pi 全体を道連れにしない」ことだけ。

crontab 例（5分おき）:
    */5 * * * * cd ~/IoT && venv/bin/python scripts/memory_guard.py >> logs/cron.log 2>&1
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVICE = "iot-monitor"

# 再起動する RSS のしきい値。
# 実測: 平常は 1000〜1150MB、OOM の引き金になったときは 1344MB、
# 記録上の最大は 1563MB。平常より上で、OOM に至る前の値にする。
RSS_LIMIT_MB = 1300

# システム全体の余裕がこれを下回ったら、RSS がしきい値未満でも手を打つ。
# OOM は「誰かが要求した瞬間」に起きるので、枯れる前に動く必要がある。
SYSTEM_AVAILABLE_FLOOR_MB = 300
# ただし iot-monitor が主犯でないなら再起動しても無駄なので、下限も見る。
RSS_FLOOR_MB = 800

# 再起動の最短間隔。短いと、起動直後の一時的な増加で再起動を繰り返す。
MIN_RESTART_INTERVAL_SECONDS = 30 * 60

STATE_PATH = ROOT / "data" / "memory_guard_last_restart"


def _main_pid() -> int | None:
    try:
        out = subprocess.run(
            ["systemctl", "show", SERVICE, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        pid = int(out or 0)
        return pid or None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def _cgroup_rss_mb(pid: int) -> float | None:
    """サービス全体（子プロセス込み）の RSS を MB で返す。

    MainPID だけ見ると、子プロセスが抱えている分を取りこぼす。
    cgroup の memory.current は memory コントローラが無効なので使えないため、
    プロセスツリーを辿って合算する。
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "--ppid", str(pid)],
            capture_output=True, text=True, timeout=10,
        ).stdout
        children = sum(int(x) for x in out.split() if x.isdigit())
        own = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not own:
            return None
        return (int(own) + children) / 1024
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def _available_mb() -> float | None:
    """MemAvailable。free が返す「実際に使える量」。"""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def _seconds_since_last_restart() -> float:
    try:
        return time.time() - STATE_PATH.stat().st_mtime
    except OSError:
        return float("inf")


def _mark_restarted() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(datetime.now().isoformat(), encoding="utf-8")
    except OSError as e:
        print(f"状態ファイルの書き込みに失敗（続行）: {e}")


def decide(rss_mb: float | None, available_mb: float | None) -> str | None:
    """再起動する理由を返す。しなくてよければ None。

    純粋関数にしてテストで境界を固定する。
    """
    if rss_mb is None:
        return None
    if rss_mb >= RSS_LIMIT_MB:
        return f"RSS {rss_mb:.0f}MB がしきい値 {RSS_LIMIT_MB}MB を超えた"
    if (
        available_mb is not None
        and available_mb <= SYSTEM_AVAILABLE_FLOOR_MB
        and rss_mb >= RSS_FLOOR_MB
    ):
        return (
            f"システムの空きが {available_mb:.0f}MB まで減り、"
            f"iot-monitor が {rss_mb:.0f}MB を占めている"
        )
    return None


def _notify(reason: str, rss_mb: float) -> None:
    """admin に知らせる。**黙って再起動しない。**

    再起動が増えているなら、それは本体のメモリ問題が悪化している合図。
    通知が来なくなれば直った合図になる。
    """
    try:
        from src.notifier import send_actionable_notification

        send_actionable_notification(
            "memory_guard_restart",
            datetime.now().strftime("%Y-%m-%d_%H%M"),
            "♻️ 見守りプロセスを再起動しました\n\n"
            f"理由: {reason}\n\n"
            "Pi 全体が落ちるのを防ぐための自動処置です。"
            "見守りの状態は引き継がれています。",
        )
    except Exception as e:
        print(f"通知に失敗（再起動自体は実施）: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="判定だけ行い、再起動しない")
    args = ap.parse_args()

    pid = _main_pid()
    if pid is None:
        print(f"{SERVICE} が動いていない → 何もしない")
        return 0

    rss = _cgroup_rss_mb(pid)
    avail = _available_mb()
    reason = decide(rss, avail)

    print(
        f"{SERVICE} RSS={rss:.0f}MB " if rss is not None else f"{SERVICE} RSS=不明 ",
        end="",
    )
    print(f"/ 空き={avail:.0f}MB " if avail is not None else "/ 空き=不明 ", end="")
    print(f"→ {reason or '正常範囲'}")

    if reason is None:
        return 0

    elapsed = _seconds_since_last_restart()
    if elapsed < MIN_RESTART_INTERVAL_SECONDS:
        print(f"前回の再起動から {elapsed / 60:.0f}分しか経っていない → 見送り")
        return 0

    if args.dry_run:
        print("--dry-run のため再起動しない")
        return 0

    print(f"{SERVICE} を再起動する")
    r = subprocess.run(
        ["systemctl", "restart", SERVICE], capture_output=True, text=True, timeout=120
    )
    if r.returncode != 0:
        print(f"再起動に失敗: {r.stderr.strip()[:200]}")
        return 1
    _mark_restarted()
    _notify(reason, rss or 0.0)
    print("再起動した")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
