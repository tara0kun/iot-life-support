"""H100ハブのIPを動的に解決する。

DHCPリース更新でIPが変わると静的設定の HUB_IP が陳腐化するため、
1. キャッシュIP → 2. 設定IP → 3. ブロードキャスト探索 の順で生きてるIPを返す。
解決できたIPは次回の高速化のためファイルにキャッシュする。
"""
from __future__ import annotations

import logging
from pathlib import Path

from kasa import Credentials, Device, Discover

log = logging.getLogger("sensors.hub_discovery")

CACHE_PATH = Path("data/discovered_hub_ip.txt")
HUB_MODEL = "H100"
PROBE_TIMEOUT = 5


async def _probe(ip: str, creds: Credentials) -> Device | None:
    try:
        dev = await Discover.discover_single(ip, credentials=creds, timeout=PROBE_TIMEOUT)
        await dev.update()
        if HUB_MODEL in (dev.model or ""):
            return dev
    except Exception:
        pass
    return None


def _read_cache() -> str | None:
    if not CACHE_PATH.exists():
        return None
    try:
        return CACHE_PATH.read_text().strip() or None
    except Exception:
        return None


def _write_cache(ip: str) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(ip)
    except Exception as e:
        log.warning("HubIPキャッシュ書込失敗: %s", e)


async def resolve_hub(configured_ip: str, username: str, password: str) -> Device | None:
    """H100に接続できるIPを探して、接続済みDeviceを返す。

    Order: キャッシュ → .env設定 → ブロードキャスト探索。
    見つけたIPはキャッシュ更新。
    """
    creds = Credentials(username, password)
    tried: set[str] = set()

    for ip in (_read_cache(), configured_ip):
        if not ip or ip in tried:
            continue
        tried.add(ip)
        dev = await _probe(ip, creds)
        if dev:
            log.info("H100接続 (cached/configured IP=%s)", ip)
            _write_cache(ip)
            return dev

    log.warning("H100が既知IP (%s) に応答せず。ブロードキャスト探索を実施", tried)
    try:
        devices = await Discover.discover(credentials=creds, discovery_timeout=5)
    except Exception as e:
        log.error("ブロードキャスト探索失敗: %s", e)
        return None

    for ip, dev in devices.items():
        try:
            await dev.update()
        except Exception:
            continue
        if HUB_MODEL in (dev.model or ""):
            log.info("H100発見 (discovered IP=%s, alias=%s)", ip, dev.alias)
            _write_cache(ip)
            return dev

    log.error("ブロードキャスト探索でもH100が見つかりません (発見デバイス: %s)",
              [(ip, d.model) for ip, d in devices.items()])
    return None
