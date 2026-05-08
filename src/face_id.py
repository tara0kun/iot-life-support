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
CANDIDATES_DIR = FACES_DIR / "candidates"
CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
CANDIDATES_INDEX = CANDIDATES_DIR / "index.json"

TOLERANCE = 0.5
# 受動収集: 未識別顔の自動保存設定
CANDIDATE_SAVE_INTERVAL_SEC = 60  # 同一未識別顔を連続保存しない最小間隔
CANDIDATE_MAX_PER_DAY = 200       # 1日の保存上限（ストレージ保護）

# 顔の最小サイズ（pixel）— これ未満は誤検出（カーテンの模様や影など）として除外
CANDIDATE_MIN_FACE_SIZE = 80
# 既存候補との類似度しきい値: この距離以下は「同一人物の重複」として保存しない
CANDIDATE_DEDUP_DISTANCE = 0.4


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

    def identify(self, image: np.ndarray, save_unknown: bool = True) -> list[dict]:
        """BGR画像から人物を識別する。

        save_unknown=True (default): 未識別の顔は data/faces/candidates/ に自動保存。
        後で家族UIから遠隔ラベル付け→学習データ追加できる（受動的学習）。

        戻り値: [{"person_id": int|None, "name": str, "confidence": float, "location": tuple}, ...]
        """
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb, number_of_times_to_upsample=2)
        if not locations:
            return []
        encodings = face_recognition.face_encodings(rgb, locations)

        results = []
        unknown_to_save: list[tuple[np.ndarray, tuple, list[float], float]] = []
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
                if save_unknown:
                    unknown_to_save.append((image, loc, enc.tolist(), float(best_dist)))

        if save_unknown and unknown_to_save:
            self._save_unknown_candidates(unknown_to_save)

        return results

    def _save_unknown_candidates(
        self,
        items: list[tuple[np.ndarray, tuple, list[float], float]],
    ) -> None:
        """未識別の顔を data/faces/candidates/ に保存。後でUIから遠隔ラベル付け可能。

        フィルタ:
        - 顔サイズが小さすぎる（< CANDIDATE_MIN_FACE_SIZE px）→ 誤検出として除外
        - 既存候補と類似（距離 < CANDIDATE_DEDUP_DISTANCE）→ 重複として除外
        """
        from datetime import datetime
        import time
        # 連続保存抑制（前回保存から N 秒経過していないとスキップ）
        if hasattr(self, "_last_candidate_save"):
            if time.time() - self._last_candidate_save < CANDIDATE_SAVE_INTERVAL_SEC:
                return

        # 当日の保存数チェック
        today = datetime.now().strftime("%Y-%m-%d")
        today_pattern = f"{today}_*.jpg"
        try:
            today_count = len(list(CANDIDATES_DIR.glob(today_pattern)))
            if today_count >= CANDIDATE_MAX_PER_DAY:
                log.info("候補顔の1日上限到達(%d) → 保存スキップ", today_count)
                return
        except Exception:
            pass

        # 既存インデックス読込
        try:
            index = json.loads(CANDIDATES_INDEX.read_text()) if CANDIDATES_INDEX.exists() else []
        except Exception:
            index = []

        # 既存候補の encoding をまとめておく（重複判定用）
        existing_encodings = []
        for c in index:
            enc = c.get("encoding")
            if enc:
                try:
                    existing_encodings.append(np.array(enc))
                except Exception:
                    pass

        ts_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        saved_count = 0
        for i, (image, loc, encoding, best_dist) in enumerate(items):
            try:
                top, right, bottom, left = loc
                face_w = right - left
                face_h = bottom - top
                # フィルタ1: 顔サイズが小さすぎる → 誤検出
                if face_w < CANDIDATE_MIN_FACE_SIZE or face_h < CANDIDATE_MIN_FACE_SIZE:
                    log.info(
                        "[face_candidate] サイズ不足で除外 %dx%d (< %d px)",
                        face_w, face_h, CANDIDATE_MIN_FACE_SIZE,
                    )
                    continue

                # フィルタ2: 既存候補との類似度チェック（重複除外）
                if existing_encodings:
                    try:
                        enc_arr = np.array(encoding)
                        distances = face_recognition.face_distance(existing_encodings, enc_arr)
                        min_d = float(np.min(distances)) if len(distances) > 0 else 1.0
                        if min_d < CANDIDATE_DEDUP_DISTANCE:
                            log.info(
                                "[face_candidate] 既存候補と類似 (dist=%.2f < %.2f) → 重複として除外",
                                min_d, CANDIDATE_DEDUP_DISTANCE,
                            )
                            continue
                    except Exception as e:
                        log.warning("候補顔の重複チェック失敗: %s", e)

                # 余白付きでクロップ
                pad = 30
                t = max(0, top - pad)
                b = min(image.shape[0], bottom + pad)
                l = max(0, left - pad)
                r = min(image.shape[1], right + pad)
                crop = image[t:b, l:r]
                fname = f"{ts_str}_{i}.jpg"
                fpath = CANDIDATES_DIR / fname
                cv2.imwrite(str(fpath), crop)
                index.append({
                    "file": fname,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "encoding": encoding,
                    "closest_distance": best_dist,
                    "face_size": [face_w, face_h],
                })
                # 直前に追加した encoding は次の重複判定にも使う（同フレーム内の重複も防げる）
                existing_encodings.append(np.array(encoding))
                saved_count += 1
            except Exception as e:
                log.warning("候補顔保存失敗: %s", e)

        if saved_count > 0:
            try:
                CANDIDATES_INDEX.write_text(json.dumps(index, ensure_ascii=False))
                log.info("[face_candidate] 候補顔 %d件 を保存", saved_count)
            except Exception as e:
                log.warning("候補顔インデックス保存失敗: %s", e)

        self._last_candidate_save = time.time()

    def register_from_encoding(
        self, person_id: int, name: str, encoding: list[float]
    ) -> bool:
        """事前計算済みのencoding（候補顔から）を登録。"""
        try:
            self._known_encodings.append(list(encoding))
            self._known_person_ids.append(person_id)
            self._known_names.append(name)
            self._save()
            log.info("候補顔から登録: %s (person_id=%d)", name, person_id)
            return True
        except Exception as e:
            log.warning("候補顔登録失敗: %s", e)
            return False

    @property
    def registered_count(self) -> int:
        return len(self._known_encodings)
