"""data/faces/candidates/ の既存候補顔をフィルタしてクリーンアップ。

実行内容:
- 顔サイズ不足の候補を削除（誤検出）
- 類似する候補は1つだけ残す（重複削除）
- インデックスを最新化

使い方:
  venv/bin/python scripts/clean_face_candidates.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.face_id import (
    CANDIDATES_DIR, CANDIDATES_INDEX,
    CANDIDATE_MIN_FACE_SIZE, CANDIDATE_DEDUP_DISTANCE,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="削除せず結果のみ表示")
    p.add_argument("--min-size", type=int, default=CANDIDATE_MIN_FACE_SIZE,
                   help=f"顔の最小サイズ (px、デフォルト={CANDIDATE_MIN_FACE_SIZE})")
    p.add_argument("--dedup-distance", type=float, default=CANDIDATE_DEDUP_DISTANCE,
                   help=f"重複判定の距離閾値 (デフォルト={CANDIDATE_DEDUP_DISTANCE})")
    args = p.parse_args()

    if not CANDIDATES_INDEX.exists():
        print("候補リストなし")
        return

    import face_recognition
    import cv2

    index = json.loads(CANDIDATES_INDEX.read_text())
    print(f"既存候補: {len(index)} 件")

    # face_size を index から、なければ画像から再計算
    enriched = []
    for c in index:
        enc = c.get("encoding")
        if not enc:
            print(f"  [SKIP] encoding なし: {c.get('file')}")
            continue
        face_size = c.get("face_size")
        if not face_size:
            # 画像から計算
            img_path = CANDIDATES_DIR / c["file"]
            if not img_path.exists():
                continue
            img = cv2.imread(str(img_path))
            if img is None:
                continue
            face_size = [img.shape[1], img.shape[0]]  # crop全体サイズで代用
        enriched.append({**c, "face_size": face_size, "_enc_arr": np.array(enc)})

    # フィルタ1: サイズ不足
    size_filtered = []
    for c in enriched:
        w, h = c["face_size"][0], c["face_size"][1]
        if w < args.min_size or h < args.min_size:
            print(f"  [SIZE] 削除候補: {c['file']} ({w}x{h})")
            continue
        size_filtered.append(c)
    print(f"サイズフィルタ後: {len(size_filtered)} 件")

    # フィルタ2: 重複除外（greedy）
    kept = []
    kept_encs = []
    for c in size_filtered:
        if kept_encs:
            try:
                distances = face_recognition.face_distance(kept_encs, c["_enc_arr"])
                if len(distances) > 0 and float(np.min(distances)) < args.dedup_distance:
                    print(f"  [DUP]  削除候補: {c['file']} (近い既存=dist={float(np.min(distances)):.2f})")
                    continue
            except Exception as e:
                print(f"  [WARN] 距離計算失敗 {c['file']}: {e}")
        kept.append(c)
        kept_encs.append(c["_enc_arr"])

    print(f"重複除外後: {len(kept)} 件")
    print(f"削除予定: {len(enriched) - len(kept)} 件")

    if args.dry_run:
        print("\n[DRY-RUN] 実際の削除はしませんでした")
        return

    # 削除実行
    keep_files = {c["file"] for c in kept}
    deleted = 0
    for c in enriched:
        if c["file"] not in keep_files:
            try:
                (CANDIDATES_DIR / c["file"]).unlink(missing_ok=True)
                deleted += 1
            except Exception as e:
                print(f"  [ERR] 削除失敗 {c['file']}: {e}")

    # _enc_arr は JSON 化できないので除外
    new_index = [{k: v for k, v in c.items() if not k.startswith("_")} for c in kept]
    CANDIDATES_INDEX.write_text(json.dumps(new_index, ensure_ascii=False))
    print(f"\n[DONE] 削除 {deleted} 件 / 保持 {len(kept)} 件")


if __name__ == "__main__":
    main()
