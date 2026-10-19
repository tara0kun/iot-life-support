"""接触センサーの取り込みで、書込失敗が2イベントを無言で消さないこと（Issue #22）。

以前は状態更新がコールバックより後ろにあったため、DB 書き込みが
`database is locked` で落ちると `_last_state` が取り残され、続く逆方向の
遷移まで「変化なし」と判定されて消えていた。1回の失敗で2イベント失う。

実例 (2026-09-26): ハブ側に open と close が記録されているのに、DB には
どちらも無かった。

検査するのは ContactSensorMonitor.run() の状態機械だけなので、ハブは差し替える。
python-kasa（実機専用で CI には無い）を引かずに import できるよう、
hub_discovery 側の kasa import を遅延させてある。
"""
from __future__ import annotations

import asyncio

from src.sensors.contact_sensor import (
    ContactEvent,
    ContactSensorConfig,
    ContactSensorMonitor,
)


class FakeContact:
    def __init__(self, is_open: bool):
        self.is_open = is_open


class FakeChild:
    def __init__(self, dev_id: str, alias: str, is_open: bool):
        self.device_id = dev_id
        self.alias = alias
        self.modules = {"ContactSensor": FakeContact(is_open)}


class FakeHub:
    """update() のたびに次の状態へ進むハブ。"""

    def __init__(self, states: list[list[bool]], aliases: list[str]):
        self._states = states
        self._aliases = aliases
        self._i = -1
        self.children: list[FakeChild] = []
        self.update_count = 0

    async def update(self):
        self.update_count += 1
        self._i = min(self._i + 1, len(self._states) - 1)
        self.children = [
            FakeChild(f"dev{n}", a, s)
            for n, (a, s) in enumerate(zip(self._aliases, self._states[self._i]))
        ]


def _run_polls(monitor: ContactSensorMonitor, hub: FakeHub, polls: int) -> None:
    """run() を polls 回だけ回す。sleep は潰し、ハブ接続は差し替える。"""

    async def body():
        async def fake_connect():
            return hub

        monitor._connect_hub = fake_connect  # type: ignore[method-assign]

        count = {"n": 0}

        async def fake_sleep(_sec):
            count["n"] += 1
            if count["n"] >= polls:
                monitor.stop()

        import src.sensors.contact_sensor as cs

        orig = cs.asyncio.sleep
        cs.asyncio.sleep = fake_sleep
        try:
            await asyncio.wait_for(monitor.run(), timeout=10)
        finally:
            cs.asyncio.sleep = orig

    asyncio.run(body())


def _monitor(on_change):
    return ContactSensorMonitor(
        cfg=ContactSensorConfig(hub_ip="1.2.3.4", username="u", password="p"),
        on_change=on_change,
    )


def test_write_failure_loses_only_that_one_event():
    """書込失敗は1件のみ失い、続く逆方向の遷移は拾える。"""
    seen: list[tuple[str, bool]] = []
    fail_on = {"n": 0}

    async def on_change(ev: ContactEvent):
        fail_on["n"] += 1
        if fail_on["n"] == 1:
            raise RuntimeError("database is locked を模擬")
        seen.append((ev.alias, ev.is_open))

    # 閉 → 開（1件目、書込失敗）→ 閉（2件目、拾えるべき）
    hub = FakeHub([[False], [True], [False]], ["トイレ"])
    m = _monitor(on_change)
    _run_polls(m, hub, polls=4)

    # 1件目は失うが、2件目の close は記録されている
    assert seen == [("トイレ", False)], f"close が消えている: {seen}"


def test_one_device_failure_does_not_skip_others():
    """1台の失敗で、同じ周回の他の子機が読み飛ばされないこと。"""
    seen: list[str] = []

    async def on_change(ev: ContactEvent):
        if ev.alias == "トイレ":
            raise RuntimeError("database is locked を模擬")
        seen.append(ev.alias)

    # 2台が同時に遷移する。先頭(トイレ)が失敗しても後続(冷蔵庫)は届くべき
    hub = FakeHub([[False, False], [True, True]], ["トイレ", "冷蔵庫"])
    m = _monitor(on_change)
    _run_polls(m, hub, polls=3)

    assert "冷蔵庫" in seen, f"後続の子機が読み飛ばされている: {seen}"


def test_failure_does_not_trigger_hub_reconnect():
    """書込失敗でハブの再接続が走らないこと（取得は成功しているので無意味）。"""
    reconnects = {"n": 0}

    async def on_change(ev: ContactEvent):
        raise RuntimeError("database is locked を模擬")

    hub = FakeHub([[False], [True]], ["トイレ"])
    m = _monitor(on_change)

    async def body():
        async def fake_connect():
            reconnects["n"] += 1
            return hub

        m._connect_hub = fake_connect  # type: ignore[method-assign]
        count = {"n": 0}

        async def fake_sleep(_sec):
            count["n"] += 1
            if count["n"] >= 3:
                m.stop()

        import src.sensors.contact_sensor as cs

        orig = cs.asyncio.sleep
        cs.asyncio.sleep = fake_sleep
        try:
            await asyncio.wait_for(m.run(), timeout=10)
        finally:
            cs.asyncio.sleep = orig

    asyncio.run(body())

    # 初回接続の1回だけ。失敗のたびに再接続していたら2回以上になる
    assert reconnects["n"] == 1, f"再接続が走っている: {reconnects['n']}回"


def test_normal_transitions_still_recorded():
    """通常の open → close が従来どおり記録されること（退行防止）。"""
    seen: list[bool] = []

    async def on_change(ev: ContactEvent):
        seen.append(ev.is_open)

    hub = FakeHub([[False], [True], [False]], ["トイレ"])
    m = _monitor(on_change)
    _run_polls(m, hub, polls=4)

    assert seen == [True, False]
