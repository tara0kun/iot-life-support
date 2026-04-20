"""カメラから顔を登録するスクリプト。

使い方:
    python scripts/register_face.py --person-id 1 --name 祖母
    python scripts/register_face.py --person-id 2 --name 母
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
from src.face_id import FaceIdentifier
from scripts.discover import load_env


def main():
    parser = argparse.ArgumentParser(description="カメラから顔を登録")
    parser.add_argument("--person-id", type=int, required=True, help="persons テーブルのID")
    parser.add_argument("--name", required=True, help="表示名（例: 祖母、母）")
    parser.add_argument("--image", help="画像ファイルパス（省略時はカメラから撮影）")
    args = parser.parse_args()

    identifier = FaceIdentifier()

    if args.image:
        img = cv2.imread(args.image)
        if img is None:
            print(f"画像を読み込めません: {args.image}")
            sys.exit(1)
    else:
        env = load_env()
        camera_ip = env.get("CAMERA_IP", "")
        camera_user = env.get("CAMERA_USERNAME", "")
        camera_pass = env.get("CAMERA_PASSWORD", "")
        # 高画質ストリームを使用（顔認識精度のため）
        rtsp_url = f"rtsp://{camera_user}:{camera_pass}@{camera_ip}:554/stream1"
        print(f"カメラに接続中... ({camera_ip})")
        cap = cv2.VideoCapture(rtsp_url)
        if not cap.isOpened():
            print("カメラに接続できません")
            sys.exit(1)
        # 数フレーム読み飛ばして安定させる
        for _ in range(5):
            cap.read()
        ret, img = cap.read()
        cap.release()
        if not ret:
            print("フレーム取得失敗")
            sys.exit(1)
        print(f"フレーム取得: {img.shape[1]}x{img.shape[0]}")

    ok = identifier.register(args.person_id, args.name, img)
    if ok:
        print(f"✅ 顔登録成功: {args.name} (person_id={args.person_id})")
        print(f"   登録済み: {identifier.registered_count}人")
    else:
        print("❌ 顔が検出されませんでした。カメラに顔を向けてもう一度試してください。")
        sys.exit(1)


if __name__ == "__main__":
    main()
