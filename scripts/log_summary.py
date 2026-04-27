"""ログ集約スクリプト。

直近24時間（または指定範囲）のsystemdジャーナル＋ logs/ から
ERROR / Exception / WARNING を抽出して整形表示する。

使い方:
    python scripts/log_summary.py                # 直近24時間
    python scripts/log_summary.py --hours 6      # 直近6時間
    python scripts/log_summary.py --notify       # サマリーをLINE送信
    python scripts/log_summary.py --service iot-monitor  # 特定サービスのみ
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
SERVICES = ["iot-web", "iot-matter", "iot-monitor"]
PATTERNS = re.compile(r"(error|exception|traceback|warning|failed|critical)", re.IGNORECASE)


def collect_systemd(service: str, since: str) -> list[str]:
    try:
        r = subprocess.run(
            ["journalctl", "-u", service, "--since", since, "--no-pager", "-q"],
            capture_output=True, text=True, timeout=30,
        )
        return [l for l in r.stdout.splitlines() if PATTERNS.search(l)]
    except Exception as e:
        return [f"(journalctl失敗 for {service}: {e})"]


def collect_log_files(hours: int) -> list[tuple[str, str]]:
    """logs/*.log から最近の問題行を集める。"""
    cutoff = datetime.now() - timedelta(hours=hours)
    out: list[tuple[str, str]] = []
    for f in LOG_DIR.glob("*.log"):
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for line in text.splitlines()[-2000:]:  # 末尾2000行のみ
            if PATTERNS.search(line):
                out.append((f.name, line))
    return out


def categorize(lines: list[str]) -> Counter:
    """エラー種別を雑にカウント（先頭の英単語をキーに）。"""
    c = Counter()
    for line in lines:
        m = re.search(r"(Error|Exception|Warning|Failed|Critical)\w*", line, re.IGNORECASE)
        if m:
            c[m.group(0).lower()] += 1
        else:
            c["その他"] += 1
    return c


def main():
    parser = argparse.ArgumentParser(description="ログ集約")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--service", default=None, help="特定サービスのみ")
    parser.add_argument("--notify", action="store_true", help="LINEに集約結果を送信")
    parser.add_argument("--max-samples", type=int, default=5)
    args = parser.parse_args()

    since_iso = (datetime.now() - timedelta(hours=args.hours)).isoformat(timespec="seconds")
    services = [args.service] if args.service else SERVICES

    sections = []
    total_count = 0

    for svc in services:
        lines = collect_systemd(svc, since_iso)
        cat = categorize(lines)
        if not lines:
            sections.append(f"## {svc}: 異常なし ✅")
            continue
        total_count += len(lines)
        head = f"## {svc}: {len(lines)}件 ({', '.join(f'{k}={v}' for k, v in cat.most_common())})"
        sample = "\n".join(f"  • {l[:200]}" for l in lines[-args.max_samples:])
        sections.append(f"{head}\n{sample}")

    file_lines = collect_log_files(args.hours)
    if file_lines:
        cat = categorize([l for _, l in file_lines])
        total_count += len(file_lines)
        sample = "\n".join(f"  • [{f}] {l[:180]}" for f, l in file_lines[-args.max_samples:])
        sections.append(
            f"## logs/ ファイル: {len(file_lines)}件 "
            f"({', '.join(f'{k}={v}' for k, v in cat.most_common())})\n{sample}"
        )

    output = f"📋 ログ集約 (直近{args.hours}時間, {datetime.now().strftime('%m/%d %H:%M')})\n\n"
    output += "\n\n".join(sections)
    output += f"\n\n合計: {total_count}件"

    print(output)

    if args.notify:
        from src.notifier import send_line_message
        # LINEメッセージは1通あたり5000文字制限。長い場合は要約。
        if len(output) > 4500:
            output = output[:4400] + "\n\n... (省略)"
        send_line_message(output)
        print("\nLINE送信完了")


if __name__ == "__main__":
    main()
