"""Tapo C220カメラの接続テスト。

1. pytapoで基本情報を取得
2. RTSPストリームからフレームを1枚取得して保存
"""
import sys
import os
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.discover import load_env

env = load_env()
CAMERA_IP = env.get("CAMERA_IP", "")
USERNAME = env.get("CAMERA_USERNAME", "")
PASSWORD = env.get("CAMERA_PASSWORD", "")

# --- pytapo で情報取得 ---
print(f"=== pytapo: {CAMERA_IP} に接続中... ===")
try:
    from pytapo import Tapo
    tapo = Tapo(CAMERA_IP, USERNAME, PASSWORD)
    info = tapo.getBasicInfo()
    device_info = info.get("device_info", {}).get("basic_info", {})
    print(f"  モデル: {device_info.get('device_model', '?')}")
    print(f"  エイリアス: {device_info.get('device_alias', '?')}")
    print(f"  FW: {device_info.get('sw_version', '?')}")
    print("  pytapo接続: OK")
except Exception as e:
    print(f"  pytapo接続失敗: {e}")

# --- RTSP で映像取得 ---
print(f"\n=== RTSP: 映像フレーム取得中... ===")
try:
    import cv2
    rtsp_url = f"rtsp://{USERNAME}:{PASSWORD}@{CAMERA_IP}:554/stream2"
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        print("  RTSPストリームを開けませんでした")
        print("  （Tapoアプリ → カメラ設定 → 高度な設定 → カメラアカウント で")
        print("   ユーザー名とパスワードを設定してください）")
        sys.exit(1)

    ret, frame = cap.read()
    cap.release()

    if ret:
        out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "test_frame.jpg")
        cv2.imwrite(out_path, frame)
        h, w = frame.shape[:2]
        print(f"  解像度: {w}x{h}")
        print(f"  フレーム保存: {out_path}")
        print("  RTSP接続: OK")
    else:
        print("  フレーム取得失敗")
except Exception as e:
    print(f"  RTSP接続失敗: {e}")
