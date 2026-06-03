"""メモリリーク調査用の tracemalloc プロファイラ。

監視タスクと並走して、定期的にメモリスナップショットを取り
ベースライン (起動5分後) との差分 TOP20 を logs/memory_profile.log に書く。

使い方:
    1. .env に `MEMORY_PROFILE=1` を追加（モジュール先頭で読み込む）
    2. iot-monitor を再起動
    3. 数時間放置
    4. `python scripts/analyze_memory.py` で増加トレンドを見る

書き出すたびに前回スナップショットと baseline 両方との差分を出すため、
「起動直後にだけ確保されたが解放されないもの」と「ジワジワ増え続けるもの」を区別できる。
"""
from __future__ import annotations

import asyncio
import logging
import resource
import tracemalloc
from datetime import datetime
from pathlib import Path

log = logging.getLogger("memory_profile")
LOG_PATH = Path("logs/memory_profile.log")
BASELINE_DELAY_SEC = 300  # 起動から5分後を baseline に
SNAPSHOT_INTERVAL_SEC = 1800  # 30分ごとにスナップショット
TOP_N = 25


def start_tracing() -> None:
    """tracemalloc 開始。プロセス起動の最も早い段階で呼ぶ。"""
    if not tracemalloc.is_tracing():
        tracemalloc.start(25)  # スタックトレース深さ25フレーム
        log.info("tracemalloc 開始 (depth=25)")


def _rss_mb() -> float:
    # Linux: ru_maxrss は KB 単位
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _format_stat(stat: tracemalloc.Statistic | tracemalloc.StatisticDiff) -> str:
    frame = stat.traceback[0]
    # ファイル名を相対化
    fname = frame.filename
    if "/IoT/" in fname:
        fname = fname.split("/IoT/", 1)[1]
    if isinstance(stat, tracemalloc.StatisticDiff):
        return (
            f"  Δ{stat.size_diff/1024:+8.1f} KB "
            f"(cur {stat.size/1024:8.1f} KB, "
            f"Δcount {stat.count_diff:+d}, cur count {stat.count}) "
            f"{fname}:{frame.lineno}"
        )
    return (
        f"  {stat.size/1024:8.1f} KB "
        f"(count {stat.count}) "
        f"{fname}:{frame.lineno}"
    )


def _write_section(label: str, body_lines: list[str]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"\n=== {ts} [{label}] RSS={_rss_mb():.1f} MB ===\n")
        for line in body_lines:
            f.write(line + "\n")
        f.flush()


async def run_profiler() -> None:
    """30分ごとにスナップショット → ログ書き出し。"""
    if not tracemalloc.is_tracing():
        log.warning("tracemalloc が開始されていません。プロファイル中止")
        return

    log.info("メモリプロファイラ稼働: baseline %d秒後, snapshot %d秒間隔",
             BASELINE_DELAY_SEC, SNAPSHOT_INTERVAL_SEC)

    await asyncio.sleep(BASELINE_DELAY_SEC)
    baseline = tracemalloc.take_snapshot()
    last_snap = baseline
    _write_section(
        "baseline",
        [_format_stat(s) for s in baseline.statistics("lineno")[:TOP_N]],
    )

    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)
        snap = tracemalloc.take_snapshot()
        # baseline 比 (じわじわ増えるもの)
        vs_baseline = snap.compare_to(baseline, "lineno")[:TOP_N]
        _write_section("vs baseline (累積増加)", [_format_stat(s) for s in vs_baseline])
        # 前回比 (直近30分の増加)
        vs_last = snap.compare_to(last_snap, "lineno")[:TOP_N]
        _write_section("vs last (直近増加)", [_format_stat(s) for s in vs_last])
        last_snap = snap
