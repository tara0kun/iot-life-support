"""Tapoデバイスをローカルネットワークから探索し、IP/種別/エイリアスを一覧表示する。

使い方:
    python scripts/discover.py

事前に .env の TAPO_USERNAME / TAPO_PASSWORD を設定すること。
"""
import asyncio
import os
from pathlib import Path

from kasa import Discover


def load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


async def main() -> None:
    env = load_env()
    username = env.get("TAPO_USERNAME") or os.environ.get("TAPO_USERNAME")
    password = env.get("TAPO_PASSWORD") or os.environ.get("TAPO_PASSWORD")

    if not username or not password:
        raise SystemExit(".env に TAPO_USERNAME と TAPO_PASSWORD を設定してください")

    print("ネットワーク上のTapoデバイスを探索中...(10秒)")
    devices = await Discover.discover(username=username, password=password)

    if not devices:
        print("デバイスが見つかりませんでした。同じWi-Fiに接続しているか確認してください。")
        return

    print(f"\n{len(devices)}台のデバイスを発見:\n")
    for ip, dev in devices.items():
        try:
            await dev.update()
        except Exception as e:
            print(f"  {ip}: 接続失敗 ({e})")
            continue
        alias = getattr(dev, "alias", "(no alias)")
        model = getattr(dev, "model", "?")
        print(f"  IP={ip}  model={model}  alias={alias}")


if __name__ == "__main__":
    asyncio.run(main())
