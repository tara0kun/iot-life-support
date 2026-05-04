"""Tapo C220からRTSPで映像を取得し、人物検知を行うモジュール。

顔認識はPhase 2で実装。現段階では人物検知（OpenCV）＋フレーム取得。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable

import cv2
import numpy as np

log = logging.getLogger("sensors.camera")


@dataclass
class CameraFrame:
    timestamp: datetime
    frame: np.ndarray
    person_detected: bool
    face_count: int = 0
    identified_persons: list[dict] | None = None  # face_id.identify() の結果


@dataclass
class CameraConfig:
    ip: str
    username: str
    password: str
    stream: str = "stream2"  # stream1=高画質, stream2=低画質
    poll_interval: float = 2.0
    save_detections: bool = True
    save_dir: Path = Path("data/captures")


class CameraMonitor:
    def __init__(
        self,
        cfg: CameraConfig,
        on_person: Callable[[CameraFrame], Awaitable[None]] | None = None,
        face_identifier=None,  # FaceIdentifier instance（None なら顔識別なし、人物検知のみ）
    ):
        self.cfg = cfg
        self._on_person = on_person
        self._face_identifier = face_identifier
        self._running = False
        self._cap: cv2.VideoCapture | None = None
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self._face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self.cfg.save_dir.mkdir(parents=True, exist_ok=True)

    @property
    def rtsp_url(self) -> str:
        return (
            f"rtsp://{self.cfg.username}:{self.cfg.password}"
            f"@{self.cfg.ip}:554/{self.cfg.stream}"
        )

    def _connect(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.rtsp_url)
        if not cap.isOpened():
            raise ConnectionError(f"RTSP接続失敗: {self.cfg.ip}")
        log.info("カメラ接続: %s (%s)", self.cfg.ip, self.cfg.stream)
        return cap

    def _detect(self, frame: np.ndarray) -> tuple[bool, int]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
        )
        face_count = len(faces)
        person = face_count > 0
        if not person:
            bodies, _ = self._hog.detectMultiScale(
                frame, winStride=(8, 8), padding=(4, 4), scale=1.05
            )
            person = len(bodies) > 0
        return person, face_count

    async def run(self) -> None:
        log.info("カメラ監視開始 (%s)", self.cfg.ip)
        self._running = True
        self._cap = self._connect()

        while self._running:
            try:
                ret, frame = self._cap.read()
                if not ret:
                    log.warning("フレーム取得失敗、再接続")
                    self._cap.release()
                    await asyncio.sleep(2)
                    self._cap = self._connect()
                    continue

                person, face_count = await asyncio.to_thread(self._detect, frame)

                if person:
                    # 顔認識: 顔が検出されており face_identifier が設定されている時だけ実行
                    identified = None
                    if face_count > 0 and self._face_identifier is not None:
                        try:
                            identified = await asyncio.to_thread(
                                self._face_identifier.identify, frame
                            )
                            if identified:
                                names = [r.get("name", "?") for r in identified
                                         if r.get("person_id")]
                                if names:
                                    log.info("顔認識: %s", ", ".join(names))
                        except Exception as e:
                            log.warning("顔識別エラー: %s", e)

                    cf = CameraFrame(
                        timestamp=datetime.now(),
                        frame=frame,
                        person_detected=True,
                        face_count=face_count,
                        identified_persons=identified,
                    )
                    if self.cfg.save_detections:
                        ts = cf.timestamp.strftime("%Y%m%d_%H%M%S")
                        path = self.cfg.save_dir / f"detect_{ts}.jpg"
                        await asyncio.to_thread(cv2.imwrite, str(path), frame)
                    if self._on_person:
                        await self._on_person(cf)

            except Exception as e:
                log.warning("カメラエラー: %s", e)
                await asyncio.sleep(2)

            await asyncio.sleep(self.cfg.poll_interval)

    def stop(self) -> None:
        self._running = False
        if self._cap:
            self._cap.release()

    def capture_one(self) -> np.ndarray | None:
        cap = self._connect()
        ret, frame = cap.read()
        cap.release()
        return frame if ret else None
