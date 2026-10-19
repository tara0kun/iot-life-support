"""顔識別できないカメラ検知を間引くこと（Issue #17）。

Haar / HOG は赤外の夜間映像で誤検知が多く、人がいない深夜3時でも毎時97件
発火していた。実測で camera の76%が person_id なし、全イベントの42%を占める。

ただし health_check.py が「2時間以内に camera イベントがあるか」で
カメラの死活を見ているので、完全に捨てると死活判定が壊れる。
間引くが捨て切らない、という境界をここで固定する。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import src.monitor as monitor
from src.sensors.camera import CameraFrame


@pytest.fixture(autouse=True)
def _reset_throttles(monkeypatch):
    monkeypatch.setattr(monitor, "_last_person_detection", 0.0)
    monkeypatch.setattr(monitor, "_last_unidentified_detection", 0.0)
    monitor._camera_identification_log.clear()
    yield
    monitor._camera_identification_log.clear()


@pytest.fixture
def recorded(monkeypatch):
    """event_bus.record_event の呼び出しを捕まえる。"""
    calls: list[dict] = []

    async def fake_record(**kw):
        calls.append(kw)
        return len(calls)

    monkeypatch.setattr(monitor.event_bus, "record_event", fake_record)
    return calls


def _frame(identified=None, face_count=1):
    return CameraFrame(
        timestamp=datetime.now(),
        frame=None,
        person_detected=True,
        face_count=face_count,
        identified_persons=identified,
    )


def test_unidentified_detections_are_throttled(recorded):
    """未識別の連続検知は、間隔を空けて1件だけ残る。"""

    async def body():
        for _ in range(20):
            await monitor.on_person_detected(_frame(identified=None))

    asyncio.run(body())

    assert len(recorded) == 1
    assert recorded[0]["source"] == "camera"
    assert "person_id" not in recorded[0] or recorded[0].get("person_id") is None


def test_unidentified_still_recorded_after_the_interval(recorded, monkeypatch):
    """間隔が空けば再び記録する（死活監視が壊れない）。"""

    async def body():
        await monitor.on_person_detected(_frame(identified=None))
        # 間隔ぶん過去にずらす
        monkeypatch.setattr(
            monitor, "_last_unidentified_detection",
            monitor._last_unidentified_detection - monitor.UNIDENTIFIED_CAMERA_INTERVAL_SEC - 1,
        )
        await monitor.on_person_detected(_frame(identified=None))

    asyncio.run(body())
    assert len(recorded) == 2


def test_throttle_interval_is_well_under_health_check_threshold():
    """health_check は camera を2時間で見ている。間引き間隔はそれより十分短いこと。"""
    assert monitor.UNIDENTIFIED_CAMERA_INTERVAL_SEC < 2 * 3600 / 4


def test_identified_detections_are_not_throttled_by_unidentified(recorded):
    """未識別が続いた直後でも、顔が識別できたら必ず記録する。

    ここを共通スロットルにすると、ノイズのせいで本物の識別を取りこぼす。
    """

    async def body():
        for _ in range(5):
            await monitor.on_person_detected(_frame(identified=None))
        await monitor.on_person_detected(
            _frame(identified=[{"person_id": 1, "confidence": 0.9, "name": "テスト祖母"}])
        )

    asyncio.run(body())

    assert len(recorded) == 2
    assert recorded[-1]["person_id"] == 1
    assert recorded[-1]["confidence"] == 0.9


def test_low_confidence_is_treated_as_unidentified(recorded):
    """信頼度が低い識別は採用しない（誤って祖母に帰属させない）。"""

    async def body():
        await monitor.on_person_detected(
            _frame(identified=[{"person_id": 1, "confidence": 0.4, "name": "テスト祖母"}])
        )

    asyncio.run(body())

    assert len(recorded) == 1
    assert recorded[0].get("person_id") is None
    assert monitor._camera_identification_log == []


def test_identified_detection_updates_active_person(recorded):
    """識別できたら active_person のヒントに積む（センサー帰属に使う）。"""

    async def body():
        await monitor.on_person_detected(
            _frame(identified=[{"person_id": 3, "confidence": 0.8, "name": "テスト家族"}])
        )

    asyncio.run(body())

    assert [e[1] for e in monitor._camera_identification_log] == [3]
