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

    # Case A: Service Data あり (Indoor Meter T/i、または Outdoor 併用時)
    if sd and len(sd) >= 3:
        try:
            device_type = chr(sd[0] & 0x7f)
            if device_type in ("w", "T", "i"):
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
        except (IndexError, ValueError):
            pass  # ↓ Case B に fallthrough

    # Case B: Service Data 無し / device_type 未識別
    # W3400010 (Outdoor Meter) は Manufacturer Data のみで送信するケースがある。
    # 2026-07-17 実運用で mac_match=19, parse_ok=0 として発覚。
    # md フォーマット: MAC(6) + type(1) + flags(1) + temp_dec(1) + temp_int|sign(1) + humidity(1) + ...
    # battery は Service Data 無しでは取得不可、None で返す。
    if manufacturer_data:
        md = manufacturer_data.get(SWITCHBOT_COMPANY_ID)
        if md and len(md) >= 11:
            try:
                temp_decimal = md[8] & 0x0f
                temp_int = md[9] & 0x7f
                temp_sign = -1 if (md[9] & 0x80) == 0 else 1
                temperature = temp_sign * (temp_int + temp_decimal / 10.0)
                humidity = md[10] & 0x7f
                # sanity check (現実的な温湿度範囲か)
                if 0 <= humidity <= 100 and -40 <= temperature <= 80:
                    return MeterReading(
                        timestamp=datetime.now(),
                        temperature_c=temperature,
                        humidity_pct=int(humidity),
                        battery_pct=None,
                    )
            except (IndexError, ValueError):
                return None
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

        # **スキャンは張りっぱなしにする。poll_seconds ごとに start/stop しない。**
        #
        # 以前は 1サイクルごとに `async with BleakScanner(...)` を作り直し、
        # 全体を asyncio.wait_for で包んでいた。タイムアウトすると wait_for が
        # `async with` の内側をキャンセルするため、__aexit__ の StopDiscovery が
        # 完了しないまま抜ける。結果 BlueZ 側にディスカバリセッションが残り、
        # 以降の StartDiscovery が [org.bluez.Error.InProgress] で全部弾かれる。
        # ハング対策として入れたタイムアウトが、詰まりを作る側になっていた。
        #
        # BLE の advertisement は接続不要のブロードキャストなので、スキャナを
        # 開いたままコールバックで拾い続けるのが素直。start/stop の回数が
        # 1回だけになり、詰まりの発生源そのものが消える。
        #
        # 「静かな成功」(スキャンは動いているが advertisement が1件も来ない)
        # は例外にならないので、無受信が続いたら自分で異常として raise する。
        # task_supervisor がタスクごと作り直し、スキャナも作り直される。
        STALE_LIMIT_SECONDS = max(self.poll_seconds * 30, 300)

        last_seen = datetime.now()

        def detection_callback_wrapped(device, advertisement_data):
            nonlocal last_seen
            # 目的のMAC以外でも「電波は届いている」証拠にはなるので時刻を更新する。
            # アダプタが詰まると全デバイスの advertisement が止まるため、
            # ここが動いている限りスキャン自体は生きていると判断できる。
            last_seen = datetime.now()
            detection_callback(device, advertisement_data)

        while self._running:
            try:
                async with BleakScanner(detection_callback=detection_callback_wrapped):
                    last_seen = datetime.now()
                    while self._running:
                        await asyncio.sleep(self.poll_seconds)

                        if latest_reading and self._on_reading:
                            if latest_reading.timestamp.timestamp() != last_reading_ts:
                                last_reading_ts = latest_reading.timestamp.timestamp()
                                try:
                                    await self._on_reading(latest_reading)
                                except Exception as e:
                                    log.warning("on_reading コールバックエラー: %s", e)

                        silent = (datetime.now() - last_seen).total_seconds()
                        if silent >= STALE_LIMIT_SECONDS:
                            raise RuntimeError(
                                f"BLE advertisement が {silent:.0f}秒 届いていない"
                                "（スキャンは動作中）"
                            )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # ここを抜けるとき async with の __aexit__ は通常どおり実行され、
                # StopDiscovery が完了する。セッションを残さない。
                log.warning("BLE スキャン異常: %s（スキャナを作り直す）", e)
                if not self._running:
                    break
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
