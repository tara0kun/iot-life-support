"""指定した名前で person を作成し、stream1 から複数フレームを撮影して顔を登録する。

使い方:
  venv/bin/python scripts/register_face_bulk.py <名前> [--role family] [--frames 8] [--interval 1.5]

既存 person と同名なら ID を再利用。新規なら INSERT。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import cv2
import face_recognition
import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from src.face_id import FaceIdentifier  # noqa: E402

DB_PATH = ROOT / "data" / "iot.db"


def get_or_create_person(name: str, role: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT id FROM persons WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0]
    cur = conn.execute(
        "INSERT INTO persons (name, role) VALUES (?, ?)", (name, role)
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("name", help="登録する人物の名前")
    p.add_argument("--role", default="family")
    p.add_argument("--frames", type=int, default=8)
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--smart", action="store_true",
                   help="複数人検出時、既存の同人物 encoding に近い顔のみ登録")
    p.add_argument("--smart-tolerance", type=float, default=0.55,
                   help="--smart 時の同人物判定しきい値（緩めなら大きく）")
    args = p.parse_args()

    person_id = get_or_create_person(args.name, args.role)
    print(f"[OK] person: id={person_id}, name={args.name}")

    ip = os.environ["CAMERA_IP"]
    user = os.environ["CAMERA_USERNAME"]
    pw = os.environ["CAMERA_PASSWORD"]
    rtsp = f"rtsp://{user}:{pw}@{ip}:554/stream1"

    def open_stream() -> cv2.VideoCapture:
        print("[INFO] RTSP接続中: stream1 (高画質)")
        c = cv2.VideoCapture(rtsp)
        if not c.isOpened():
            print("[ERR] RTSP接続失敗")
            sys.exit(1)
        return c

    cap = open_stream()
    consecutive_fail = 0

    fid = FaceIdentifier()
    print(f"[INFO] 既存顔データ: {fid.registered_count}件 / smart={args.smart}")

    # smart モード: 既存の同 person_id の encoding を参照用に保持
    target_encodings: list[np.ndarray] = []
    if args.smart:
        for enc, pid in zip(fid._known_encodings, fid._known_person_ids):
            if pid == person_id:
                target_encodings.append(np.array(enc))
        print(f"[INFO] smart参照: 既存 {args.name} encoding {len(target_encodings)}件")
        if not target_encodings:
            print(f"[ERR] --smart 指定時は既存 encoding が必要です。先に通常モードで初期登録してください。")
            sys.exit(1)

    saved_dir = ROOT / "data" / "captures" / "register"
    saved_dir.mkdir(parents=True, exist_ok=True)

    success = 0
    for i in range(args.frames):
        for _ in range(5):
            cap.grab()
        ret, frame = cap.retrieve()
        if not ret:
            consecutive_fail += 1
            print(f"[WARN] フレーム取得失敗 ({i+1}/{args.frames}) 連続={consecutive_fail}")
            # 3連続失敗で再接続
            if consecutive_fail >= 3:
                print("[INFO] RTSP再接続を試行")
                cap.release()
                time.sleep(3)
                cap = open_stream()
                consecutive_fail = 0
            time.sleep(args.interval)
            continue
        consecutive_fail = 0

        ts = time.strftime("%H%M%S")
        if args.smart:
            # 全顔検出 → target に最も近い顔のみ登録
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            locs = face_recognition.face_locations(rgb, number_of_times_to_upsample=2)
            encs = face_recognition.face_encodings(rgb, locs) if locs else []
            best_idx = -1
            best_dist = 1.0
            for idx, e in enumerate(encs):
                dist = float(min(face_recognition.face_distance(target_encodings, e)))
                if dist < best_dist:
                    best_dist = dist
                    best_idx = idx
            if best_idx >= 0 and best_dist <= args.smart_tolerance:
                fid._known_encodings.append(encs[best_idx].tolist())
                fid._known_person_ids.append(person_id)
                fid._known_names.append(args.name)
                fid._save()
                snap = saved_dir / f"{args.name}_{ts}_ok_d{best_dist:.2f}.jpg"
                cv2.imwrite(str(snap), frame)
                print(f"[{i+1}/{args.frames}] 登録成功 dist={best_dist:.2f} 検出顔数={len(encs)} → {snap.name}")
                success += 1
            else:
                snap = saved_dir / f"{args.name}_{ts}_no_d{best_dist:.2f}.jpg"
                cv2.imwrite(str(snap), frame)
                reason = f"検出={len(encs)}件 最小dist={best_dist:.2f}"
                print(f"[{i+1}/{args.frames}] スキップ ({reason}) → {snap.name}")
        else:
            ok = fid.register(person_id, args.name, frame)
            snap = saved_dir / f"{args.name}_{ts}_{'ok' if ok else 'no'}.jpg"
            cv2.imwrite(str(snap), frame)
            print(f"[{i+1}/{args.frames}] {'登録成功' if ok else '顔検出なし'} → {snap.name}")
            success += int(ok)
        time.sleep(args.interval)

    cap.release()
    print(f"\n[DONE] 登録成功 {success}/{args.frames} 件 / 累計顔データ {fid.registered_count}件")


if __name__ == "__main__":
    main()
