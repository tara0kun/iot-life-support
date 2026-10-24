"""停止時に RTSP を解放すること（Issue #29 の付随部分）。

本体の修正は ExecStart の `--timeout-graceful-shutdown`。
`/ws/events` と MJPEG 配信はクライアントが切るまで終わらないレスポンスで、
uvicorn は停止時にそれを「background tasks」として待つ。タブレットが
繋ぎっぱなしだと TimeoutStopSec=5 を超え、30日で92回 SIGKILL されていた。

アプリ側の lifespan shutdown は**待機が終わった後**にしか走らないので、
ここで停止フラグを立ててもループは畳めない（実測で確認済み）。
このファイルが見るのは、その後片付けが安全に走ることだけ。
"""
from __future__ import annotations

import asyncio

import src.web.app as web


def test_shutdown_stops_the_camera_streamer(temp_db, monkeypatch):
    """停止時にカメラのストリーマーを止める（RTSP を掴んだままにしない）。"""
    stopped = []

    class FakeStreamer:
        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(web, "get_streamer", lambda: FakeStreamer())
    asyncio.run(web.shutdown())
    assert stopped == [True]


def test_shutdown_survives_streamer_failure(temp_db, monkeypatch):
    """ストリーマーの停止が失敗しても、終了処理は止めない。"""

    class BrokenStreamer:
        def stop(self):
            raise RuntimeError("RTSP の解放に失敗を模擬")

    monkeypatch.setattr(web, "get_streamer", lambda: BrokenStreamer())
    asyncio.run(web.shutdown())  # 例外が外に出ないこと


def test_unit_file_sets_graceful_shutdown_timeout():
    """**これが本体の修正。** ExecStart から外れたら停止は元の壊れ方に戻る。

    値は TimeoutStopSec より小さくないと意味がない（待ちきる前に
    systemd が SIGKILL してしまう）。
    """
    import re
    from pathlib import Path

    unit = Path(__file__).resolve().parent.parent / "systemd" / "iot-web.service"
    text = unit.read_text(encoding="utf-8")

    m = re.search(r"--timeout-graceful-shutdown\s+(\d+)", text)
    assert m, "ExecStart に --timeout-graceful-shutdown が無い"
    graceful = int(m.group(1))

    m2 = re.search(r"^TimeoutStopSec=(\d+)", text, re.MULTILINE)
    assert m2, "TimeoutStopSec が無い"
    stop_sec = int(m2.group(1))

    assert graceful < stop_sec, (
        f"--timeout-graceful-shutdown={graceful} は "
        f"TimeoutStopSec={stop_sec} より小さい必要がある"
    )
