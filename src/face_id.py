"""顔認識による人物識別モジュール。

機能:
- カメラ映像から顔を検出
- 登録済み顔データと照合して person_id を返す
- 新しい顔の登録（家族UIから）
- 顔データはdata/faces/に保存
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import cv2
import face_recognition
import numpy as np

log = logging.getLogger("face_id")

FACES_DIR = Path(__file__).resolve().parent.parent / "data" / "faces"
FACES_DIR.mkdir(parents=True, exist_ok=True)
ENCODINGS_FILE = FACES_DIR / "encodings.json"

TOLERANCE = 0.5


class FaceIdentifier:
    def __init__(self) -> None:
        self._known_encodings: list[list[float]] = []
        self._known_person_ids: list[int] = []
        self._known_names: list[str] = []
        self._load()

    def _load(self) -> None:
        if ENCODINGS_FILE.exists():
            data = json.loads(ENCODINGS_FILE.read_text())
            for entry in data:
                self._known_encodings.append(entry["encoding"])
                self._known_person_ids.append(entry["person_id"])
                self._known_names.append(entry["name"])
            log.info("顔データ読み込み: %d人分", len(self._known_encodings))

    def _save(self) -> None:
        data = []
        for enc, pid, name in zip(
            self._known_encodings, self._known_person_ids, self._known_names
        ):
            data.append({"person_id": pid, "name": name, "encoding": enc})
        ENCODINGS_FILE.write_text(json.dumps(data, ensure_ascii=False))

    def register(self, person_id: int, name: str, image: np.ndarray) -> bool:
        """BGR画像から顔を登録する。成功したらTrue。"""
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        # upsample=2: 2K画像でも小さめの顔（160px程度）を拾えるようにする
        locations = face_recognition.face_locations(rgb, number_of_times_to_upsample=2)
        if not locations:
            log.warning("顔が検出されませんでした: %s", name)
            return False
        encodings = face_recognition.face_encodings(rgb, locations)
        if not encodings:
            return False
        self._known_encodings.append(encodings[0].tolist())
        self._known_person_ids.append(person_id)
        self._known_names.append(name)
        self._save()
        # 顔画像も保存
        face_img_path = FACES_DIR / f"{person_id}_{name}.jpg"
        top, right, bottom, left = locations[0]
        face_crop = image[top:bottom, left:right]
        cv2.imwrite(str(face_img_path), face_crop)
        log.info("顔登録完了: %s (person_id=%d)", name, person_id)
        return True

    def identify(self, image: np.ndarray) -> list[dict]:
        """BGR画像から人物を識別する。

        戻り値: [{"person_id": int|None, "name": str, "confidence": float, "location": tuple}, ...]
        """
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb, number_of_times_to_upsample=2)
        if not locations:
            return []
        encodings = face_recognition.face_encodings(rgb, locations)

        results = []
        for enc, loc in zip(encodings, locations):
            if not self._known_encodings:
                results.append({
                    "person_id": None,
                    "name": "unknown",
                    "confidence": 0.0,
                    "location": loc,
                })
                continue
            distances = face_recognition.face_distance(
                [np.array(e) for e in self._known_encodings], enc
            )
            best_idx = int(np.argmin(distances))
            best_dist = distances[best_idx]
            confidence = max(0.0, 1.0 - best_dist)

            if best_dist <= TOLERANCE:
                results.append({
                    "person_id": self._known_person_ids[best_idx],
                    "name": self._known_names[best_idx],
                    "confidence": confidence,
                    "location": loc,
                })
            else:
                results.append({
                    "person_id": None,
                    "name": "unknown",
                    "confidence": confidence,
                    "location": loc,
                })

        return results

    @property
    def registered_count(self) -> int:
        return len(self._known_encodings)
