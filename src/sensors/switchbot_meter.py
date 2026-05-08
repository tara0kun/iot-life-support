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
# SwitchBot のメーカーID（little-endian で 0x0969 = 2409 decimal）
SWITCHBOT_COMPANY_ID = 0x0969


def _parse_meter_advertisement(
    service_data: dict, manufacturer_data: dict | None = None
) -> MeterReading | None:
    """SwitchBot Meter / OutdoorMeter の広告パケットを解析する。

    モデルにより温湿度の格納場所が異なる:
      - Indoor Meter (T/i): Service Data の byte3-5 に温度/湿度
      - Outdoor Meter (w, W3400010): Manufacturer Data の byte8-10 に温度/湿度
        Service Data は device_type + battery のみ（3バイト）

    Service Data フォーマット:
      byte0=device_type ('w'=屋外, 'T'/'i'=屋内)
      byte1=status flags, byte2=battery(下位7bit)

    Manufacturer Data (Outdoor): MAC(6) + 予備(2) + temp_dec(1) + temp_int|sign(1) + humidity(1)
    """
    sd = service_data.get(SWITCHBOT_SERVICE_UUID)
    if not sd or len(sd) < 3:
        return None
    try:
        device_type = chr(sd[0] & 0x7f)
        if device_type not in ("w", "T", "i"):
            return None
        battery = sd[2] & 0x7f

        # Outdoor Meter ('w'): 温湿度は Manufacturer Data に
        if device_type == "w" and manufacturer_data:
            md = manufacturer_data.get(SWITCHBOT_COMPANY_ID)
            if md and len(md) >= 11:
                temp_decimal = md[8] & 0x0f
                temp_int = md[9] & 0x7f
                temp_sign = -1 if (md[9] & 0x80) == 0 else 1
                temperature = temp_sign * (temp_int + temp_decimal / 10.0)
                humidity = md[10] & 0x7f
                return MeterReading(
                    timestamp=datetime.now(),
                    temperature_c=temperature,
                    humidity_pct=int(humidity),
                    battery_pct=int(battery) if 0 < battery <= 100 else None,
                )

        # Indoor Meter (T/i): 温湿度は Service Data の byte3-5
        if len(sd) >= 6:
            temp_decimal = sd[3] & 0x0f
            temp_int = sd[4] & 0x7f
            temp_sign = -1 if (sd[4] & 0x80) == 0 else 1
            temperature = temp_sign * (temp_int + temp_decimal / 10.0)
            humidity = sd[5] & 0x7f
            return MeterReading(
                timestamp=datetime.now(),
                temperature_c=temperature,
                humidity_pct=int(humidity),
                battery_pct=int(battery) if 0 < battery <= 100 else None,
            )
        return None
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
            r = _parse_meter_advertisement(
                advertisement_data.service_data or {},
                advertisement_data.manufacturer_data or {},
            )
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
