"""P110Mの接続テスト。複数の方法を試す。"""
import asyncio
import sys, os
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.discover import load_env

env = load_env()
IP = env.get("RICE_COOKER_IP", "192.168.x.x")
USER = env.get("TAPO_USERNAME", "")
PASS = env.get("TAPO_PASSWORD", "")

async def try_tapo_lib():
    """tapo ライブラリ (Rust製)"""
    print("=== tapo ライブラリ ===")
    try:
        from tapo import ApiClient
        client = ApiClient(USER, PASS)
        device = await client.p110(IP)
        info = await device.get_device_info()
        print(f"  モデル: {info.model}")
        print(f"  電源: {'ON' if info.device_on else 'OFF'}")
        usage = await device.get_current_power()
        print(f"  消費電力: {usage.current_power} W")
        return True
    except Exception as e:
        print(f"  失敗: {e}")
        return False

async def try_kasa_direct():
    """python-kasa: discover_single"""
    print("\n=== python-kasa (discover_single) ===")
    try:
        from kasa import Discover
        dev = await Discover.discover_single(IP, username=USER, password=PASS)
        await dev.update()
        energy = dev.modules.get("Energy")
        if energy:
            print(f"  消費電力: {energy.current_consumption} W")
        return True
    except Exception as e:
        print(f"  失敗: {e}")
        return False

async def try_kasa_connect():
    """python-kasa: Device.connect with explicit config"""
    print("\n=== python-kasa (Device.connect) ===")
    try:
        from kasa import Device, DeviceConfig, Credentials
        creds = Credentials(username=USER, password=PASS)
        config = DeviceConfig(host=IP, credentials=creds)
        dev = await Device.connect(config=config)
        await dev.update()
        energy = dev.modules.get("Energy")
        if energy:
            print(f"  消費電力: {energy.current_consumption} W")
        await dev.disconnect()
        return True
    except Exception as e:
        print(f"  失敗: {e}")
        return False

async def main():
    ok = await try_tapo_lib()
    if not ok:
        ok = await try_kasa_direct()
    if not ok:
        ok = await try_kasa_connect()
    if not ok:
        print("\n❌ すべての方法で接続できませんでした")
        print("→ 方法2（Matter接続）を試しましょう")
    else:
        print("\n✅ 接続成功！")

asyncio.run(main())
