"""家族UI用のカメラライブ配信。

iot-monitor とは別プロセスの iot-web 内で stream2 (低画質) を購読し、
最新フレームをメモリ保持。MJPEG / snapshot エンドポイントから配信する。

シングルトンとして使用:
    streamer = get_streamer()
    streamer.start()  # 初回呼出で RTSP 接続開始
    jpeg_bytes = streamer.latest_jpeg()
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import cv2
from dotenv import load_dotenv

log = logging.getLogger("web.camera_stream")

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


class CameraStreamer:
    def __init__(self, stream: str = "stream2", jpeg_quality: int = 70) -> None:
        self._ip = os.environ.get("CAMERA_IP", "")
        self._user = os.environ.get("CAMERA_USERNAME", "")
        self._pass = os.environ.get("CAMERA_PASSWORD", "")
        self._stream = stream
        self._jpeg_quality = jpeg_quality
        self._latest: bytes | None = None
        self._latest_ts: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._ip and self._user and self._pass)

    @property
    def rtsp_url(self) -> str:
        return f"rtsp://{self._user}:{self._pass}@{self._ip}:554/{self._stream}"

    def start(self) -> None:
        if self._running or not self.configured:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="CameraStreamer", daemon=True)
        self._thread.start()
        log.info("CameraStreamer 起動: %s (%s)", self._ip, self._stream)

    def stop(self) -> None:
        self._running = False

    def latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest

    def latest_age_seconds(self) -> float:
        return time.time() - self._latest_ts if self._latest_ts else 1e9

    def _loop(self) -> None:
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
        while self._running:
            cap = cv2.VideoCapture(self.rtsp_url)
            if not cap.isOpened():
                log.warning("RTSP接続失敗、5秒後に再試行")
                time.sleep(5)
                continue
            log.info("RTSP接続: %s", self._stream)
            try:
                while self._running:
                    ret, frame = cap.read()
                    if not ret:
                        log.warning("フレーム取得失敗、再接続")
                        break
                    ok, buf = cv2.imencode(".jpg", frame, encode_param)
                    if ok:
                        with self._lock:
                            self._latest = buf.tobytes()
                            self._latest_ts = time.time()
                    # MJPEG 配信用に約 5fps 出せれば十分
                    time.sleep(0.18)
            except Exception as e:
                log.warning("ストリーミング例外: %s", e)
            finally:
                cap.release()
            if self._running:
                time.sleep(2)


_singleton: CameraStreamer | None = None


def get_streamer() -> CameraStreamer:
    global _singleton
    if _singleton is None:
        _singleton = CameraStreamer()
    return _singleton
