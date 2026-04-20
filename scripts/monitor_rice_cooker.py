"""炊飯器用のPowerMonitorを起動する (手動テスト用)。

.env の TAPO_USERNAME/PASSWORD と RICE_COOKER_IP を使う。
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.power_monitor import MonitorConfig, PowerMonitor
from scripts.discover import load_env


def main() -> None:
    env = load_env()
    username = env.get("TAPO_USERNAME") or os.environ.get("TAPO_USERNAME", "")
    password = env.get("TAPO_PASSWORD") or os.environ.get("TAPO_PASSWORD", "")
    ip = env.get("RICE_COOKER_IP") or os.environ.get("RICE_COOKER_IP", "")
    threshold = float(env.get("RICE_COOKER_THRESHOLD_W", "600"))
    interval = float(env.get("POLL_INTERVAL", "5"))

    if not (username and password and ip):
        raise SystemExit(".env の TAPO_USERNAME/TAPO_PASSWORD/RICE_COOKER_IP を設定してください")

    cfg = MonitorConfig(
        name="rice_cooker",
        ip=ip,
        threshold_w=threshold,
        username=username,
        password=password,
        poll_interval=interval,
    )
    asyncio.run(PowerMonitor(cfg).run())


if __name__ == "__main__":
    main()
