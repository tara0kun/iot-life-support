"""SwitchBot 防水温湿度計を BLE 直接読取り（ハブ不要）で監視するモジュール。

SwitchBotの温湿度計は周囲にBLE Advertisementとして温度・湿度・電池残量を周期的に
broadcastしている。これを bleak ライブラリで受信して event_bus に流す。

設定（.env）:
  SWITCHBOT_METER_ENABLED=1   # 0なら本モジュールは起動しない
  SWITCHBOT_METER_MAC=XX:XX:XX:XX:XX:XX
  SWITCHBOT_METER_POLL_SECONDS=10

依存ライブラリ:
  pip install bleak

未導入なら起動時に警告ログを出してスキップする（クラッシュしない）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Awaitable

log = logging.getLogger("sensors.switchbot_meter")


@dataclass
class MeterReading:
    timestamp: datetime
    temperature_c: float
    humidity_pct: int
    battery_pct: int | None = None


# SwitchBot 温湿度計（防水版含む）の Service Data UUID（ベンダー固有）
SWITCHBOT_SERVICE_UUID = "0000fd3d-0000-1000-8000-00805f9b34fb"


def _parse_meter_advertisement(service_data: dict) -> MeterReading | None:
    """SwitchBot Meter / OutdoorMeter の Service Data を解析する。

    フォーマット参考: https://github.com/OpenWonderLabs/SwitchBotAPI-BLE
    Outdoor Meter (W3400010): byte0=device_type(0x77 or 'w'),
      byte1=status, byte2=battery, byte3-5=temp/humidity (各種版)
    """
    raw = service_data.get(SWITCHBOT_SERVICE_UUID)
    if not raw or len(raw) < 6:
        return None
    try:
        # device type: byte0
        device_type = chr(raw[0] & 0x7f)  # 0x77='w'(温湿度計), 0x54='T'(古い), etc.
        if device_type not in ("w", "T", "i"):
            return None
        battery = raw[2] & 0x7f
        # 温度: byte3 が小数, byte4 が整数（下位7bit）, 符号は byte4 上位bit
        temp_decimal = raw[3] & 0x0f
        temp_int = raw[4] & 0x7f
        temp_sign = -1 if (raw[4] & 0x80) == 0 else 1
        temperature = temp_sign * (temp_int + temp_decimal / 10.0)
        # 湿度: byte5 下位7bit
        humidity = raw[5] & 0x7f
        return MeterReading(
            timestamp=datetime.now(),
            temperature_c=temperature,
            humidity_pct=int(humidity),
            battery_pct=int(battery) if 0 < battery <= 100 else None,
        )
    except (IndexError, ValueError):
        return None


class SwitchBotMeterMonitor:
    def __init__(
        self,
        target_mac: str,
        poll_seconds: float = 10.0,
        on_reading: Callable[[MeterReading], Awaitable[None]] | None = None,
    ):
        self.target_mac = target_mac.upper().replace("-", ":")
        self.poll_seconds = poll_seconds
        self._on_reading = on_reading
        self._running = False

    async def run(self) -> None:
        """BLE スキャンを起動。bleak未導入ならログ出力して即終了。"""
        try:
            from bleak import BleakScanner  # type: ignore
        except ImportError:
            log.warning(
                "bleak ライブラリがインストールされていません。"
                "SwitchBot 温湿度計監視はスキップします。"
                "有効化には 'pip install bleak' を実行してください。"
            )
            return

        if not self.target_mac:
            log.warning("SWITCHBOT_METER_MAC が未設定。SwitchBot監視をスキップ。")
            return

        log.info("SwitchBot 温湿度計 BLE 監視開始 (MAC=%s, 間隔=%.0fs)",
                 self.target_mac, self.poll_seconds)
        self._running = True

        last_reading_ts = 0.0
        latest_reading: MeterReading | None = None

        def detection_callback(device, advertisement_data):
            nonlocal latest_reading
            if device.address.upper() != self.target_mac:
                return
            r = _parse_meter_advertisement(advertisement_data.service_data or {})
            if r:
                latest_reading = r

        while self._running:
            try:
                async with BleakScanner(detection_callback=detection_callback) as scanner:
                    # poll_seconds 秒間スキャンし続ける
                    await asyncio.sleep(self.poll_seconds)
                if latest_reading and self._on_reading:
                    if latest_reading.timestamp.timestamp() != last_reading_ts:
                        last_reading_ts = latest_reading.timestamp.timestamp()
                        try:
                            await self._on_reading(latest_reading)
                        except Exception as e:
                            log.warning("on_reading コールバックエラー: %s", e)
            except Exception as e:
                log.warning("BLE スキャン失敗: %s（次回リトライ）", e)
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
