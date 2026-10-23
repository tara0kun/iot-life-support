"""センサーの電池切れを、黙る前に知らせること。

**これが無いと電池切れは無言の障害になる。** health_check が無反応を
検知するのは30〜48時間後で、脱衣所のモーションセンサーは入浴の確定と
「浴室で動きがない」緊急通知の両方を担っているので、黙ってから気づくのでは遅い。

2026-10-23 の点検で、実際に脱衣所が低電池だった。それまで電池を見る
コードは1行も無く、`battery_low` の参照は0件だった。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from src.sensors.contact_sensor import (
    BATTERY_CHECK_INTERVAL_SECONDS,
    BatteryStatus,
    ContactSensorConfig,
    ContactSensorMonitor,
)


class FakeFeature:
    def __init__(self, value):
        self.value = value


class FakeChild:
    def __init__(self, alias, low=False, rssi=-70, has_battery=True):
        self.alias = alias
        self.device_id = f"dev-{alias}"
        self.modules = {}
        self.features = {"rssi": FakeFeature(rssi)}
        if has_battery:
            self.features["battery_low"] = FakeFeature(low)


class FakeHub:
    def __init__(self, children):
        self.children = children


def _monitor(on_battery_low=None):
    return ContactSensorMonitor(
        cfg=ContactSensorConfig(hub_ip="1.2.3.4", username="u", password="p"),
        on_battery_low=on_battery_low,
    )


def test_低電池だけを通知する():
    seen: list[BatteryStatus] = []

    async def cb(st):
        seen.append(st)

    m = _monitor(cb)
    hub = FakeHub([
        FakeChild("脱衣所", low=True, rssi=-67),
        FakeChild("トイレ", low=False),
        FakeChild("冷蔵庫", low=False),
    ])
    asyncio.run(m._check_batteries(hub))

    assert [s.alias for s in seen] == ["脱衣所"]
    assert seen[0].is_low is True
    assert seen[0].rssi == -67


def test_電池情報が無い子機は飛ばす():
    """battery_low を公開しない機器で落ちないこと。"""
    seen = []

    async def cb(st):
        seen.append(st)

    m = _monitor(cb)
    asyncio.run(m._check_batteries(FakeHub([FakeChild("プラグ", has_battery=False)])))
    assert seen == []


def test_1台の通知失敗が他を止めない():
    """最初の1台で例外が出ても、残りの子機を見続けること。"""
    seen = []

    async def cb(st):
        if st.alias == "脱衣所":
            raise RuntimeError("通知の失敗を模擬")
        seen.append(st.alias)

    m = _monitor(cb)
    hub = FakeHub([FakeChild("脱衣所", low=True), FakeChild("トイレ", low=True)])
    asyncio.run(m._check_batteries(hub))

    assert seen == ["トイレ"]


def test_コールバック未設定でも落ちない():
    m = _monitor(None)
    asyncio.run(m._check_batteries(FakeHub([FakeChild("脱衣所", low=True)])))


def test_rssiが読めなくても通知する():
    """電波強度は付加情報。取れなくても電池の警告は出すこと。"""
    seen = []

    async def cb(st):
        seen.append(st)

    m = _monitor(cb)
    child = FakeChild("脱衣所", low=True)
    child.features["rssi"] = FakeFeature("読めない値")
    asyncio.run(m._check_batteries(FakeHub([child])))

    assert len(seen) == 1
    assert seen[0].rssi is None


def test_確認間隔は長すぎず短すぎない():
    """5秒ポーリングのたびに読むとハブに負荷がかかる。
    一方で日次より粗いと、切れる前に気づけない幅が狭まる。
    """
    assert 600 <= BATTERY_CHECK_INTERVAL_SECONDS <= 6 * 3600


def test_通知は同じ電池で日に1度だけ():
    """毎時間鳴らすと、家族が通知を無視する習慣につながる。"""
    import src.monitor as monitor

    monitor._battery_notified.clear()
    sent: list[tuple] = []

    def fake_send(cat, ctx, msg):
        sent.append((cat, ctx, msg))

    import src.notifier as notifier

    orig = notifier.send_actionable_notification
    notifier.send_actionable_notification = fake_send
    try:
        st = BatteryStatus(alias="脱衣所", device_id="d1", is_low=True, rssi=-67)
        for _ in range(5):
            asyncio.run(monitor.on_sensor_battery_low(st))
    finally:
        notifier.send_actionable_notification = orig
        monitor._battery_notified.clear()

    assert len(sent) == 1
    cat, ctx, msg = sent[0]
    assert cat == "sensor_battery_low"
    assert "脱衣所" in ctx and datetime.now().strftime("%Y-%m-%d") in ctx
    assert "CR2032" in msg


def test_通知文に止まる機能が書かれている():
    """「電池が少ない」だけでは動いてもらえない。何が止まるかを伝える。"""
    import src.monitor as monitor

    monitor._battery_notified.clear()
    sent = []

    def fake_send(cat, ctx, msg):
        sent.append(msg)

    import src.notifier as notifier

    orig = notifier.send_actionable_notification
    notifier.send_actionable_notification = fake_send
    try:
        asyncio.run(monitor.on_sensor_battery_low(
            BatteryStatus(alias="脱衣所", device_id="d1", is_low=True, rssi=-67)))
    finally:
        notifier.send_actionable_notification = orig
        monitor._battery_notified.clear()

    assert "緊急通知" in sent[0], sent[0]


def test_未知の場所でも通知できる():
    """BATTERY_IMPACT に無い alias でも落ちずに汎用文で送ること。"""
    import src.monitor as monitor

    monitor._battery_notified.clear()
    sent = []

    def fake_send(cat, ctx, msg):
        sent.append(msg)

    import src.notifier as notifier

    orig = notifier.send_actionable_notification
    notifier.send_actionable_notification = fake_send
    try:
        asyncio.run(monitor.on_sensor_battery_low(
            BatteryStatus(alias="知らない場所", device_id="d9", is_low=True, rssi=None)))
    finally:
        notifier.send_actionable_notification = orig
        monitor._battery_notified.clear()

    assert len(sent) == 1
    assert "記録が止まります" in sent[0]
