# コードベース規模と構成

> 2026-07-03 時点の `iot-life-support` のコード規模と構成メトリクス。

## LOC (Lines of Code)

| 対象 | 行数 |
|---|---:|
| `src/` (アプリ本体 Python) | **8,509** |
| `scripts/` (運用・cron・保守 Python + Shell) | **3,901** |
| **合計 (テスト・データ・生成物を除く)** | **12,410** |

## モジュール数

| ディレクトリ | ファイル数 |
|---|---:|
| `src/` 直下 (トップレベル) | 17 |
| `src/sensors/` (センサ抽象化) | 6 |
| `src/web/` (FastAPI + 配信) | 3 |
| `src/web/templates/` (HTML) | 7 |
| `scripts/` (`.py` + `.sh`) | 29 |
| **総 Python ファイル数** | **約 55** |

## 主要モジュールの粒度

### `src/` トップレベル (17ファイル)

- `monitor.py` — 中央オーケストレータ (最大級)
- `notifier.py` — LINE 送信
- `line_commands.py` — LINE Bot 双方向 (18 handler)
- `sessions.py` — セッション集約
- `event_bus.py` — asyncio pub/sub
- `event_summarizer.py` — 通知文面生成
- `bath_detector.py` — 入浴検知
- `face_id.py` — 顔認識
- `memory_profiler.py` — メモリ診断
- `db.py` — SQLite ハンドラ
- `settings.py` — DB 設定
- `security.py` — トークン検証
- `event_types.py` — 型定義
- `constants.py` — 定数
- `lock_manager.py` — Matter プラグ電源制御
- (+ その他 2)

### `src/sensors/` (6ファイル)

`camera.py` / `contact_sensor.py` / `matter_plug.py` / `switchbot_meter.py` / `hub_discovery.py` / `__init__.py`

### `src/web/` (3 + 7 templates)

Python: `app.py` (~1,300 行) / `camera_stream.py` / `__init__.py`
HTML: `tablet.html` / `family.html` / `face_learning.html` / `guide.html` / `family_manual.html` / `login.html` / `weekly_report.html`

## 稼働ユニット

### systemd (4 unit)

| ユニット | 用途 | 状態 |
|---|---|---|
| `iot-monitor.service` | Layer 1+2 (センサ+判定) | active |
| `iot-web.service` | Layer 3+4 (通知+UI, uvicorn) | active |
| `iot-matter.service` | matter-server (Matter通信中継) | active |
| `iot-tunnel.service` | Cloudflare Quick Tunnel | **disabled** (6/3廃止、Tailscale Funnel移行) |

### cron (11 ジョブ)

DB バックアップ / ヘルスチェック / 定期通知 / ログローテーション / outbox 再送 / 週次スナップショット / anomaly_check 等。詳細は [architecture.md](architecture.md#cron-11-job) 参照。

## LINE Bot コマンド

`src/line_commands.py` の `handle_*` 関数数: **18**

代表的なコマンド:
- `メニュー` / `リンク` / `URL`
- `意見` / `質問` / `要望` / `バグ` (feedback boxes)
- `ヘルプ` / `使い方`
- `登録 <名前>` (家族の LINE user 登録)
- `通知オン` / `通知オフ` (マスタースイッチ)
- 各種 postback (session_confirm / bath_classification / lock_confirm 等)

## Git 運用

| 指標 | 値 |
|---|---:|
| 総コミット数 (main + dev) | **116** |
| `dev` が `main` より先行 | **61 コミット** |
| ブランチ戦略 | `main` (本番) / `dev` (開発) / `future` (実験) |
| コミットメッセージスタイル | **Problem / Cause / Fix** を本文に記述 (障害・恒久対策コミット) |

## 外部依存 (主要)

| ライブラリ | 用途 |
|---|---|
| `fastapi` + `uvicorn` + `starlette` | Web + WebSocket |
| `python-kasa` | Tapo H100 / T110 / T100 |
| `python-matter-server` | Matter デバイス (P110M) |
| `face_recognition` (dlib) | 顔識別 |
| `opencv-python` (cv2) | 映像処理 |
| `bleak` | SwitchBot BLE 直接読取り |
| `aiohttp` | 非同期HTTP (Tapo 内部) |
| `requests` | LINE API 呼び出し |
| `python-dotenv` | .env |
| `Jinja2` | HTML テンプレート |

## デバイス構成 (物理)

| 機器 | 数 | 通信 |
|---|---:|---|
| Raspberry Pi 5 | 1 | (本体) |
| Tapo H100 (ハブ) | 1 | WiFi |
| Tapo T110 (開閉センサー) | 4 | H100 経由 |
| Tapo T100 (モーション) | 1 | H100 経由 |
| Tapo C220 (カメラ) | 1 | WiFi + RTSP |
| Tapo P110M (Matter プラグ) | 1 | Matter |
| SwitchBot 防水温湿度計 W3400010 | 1 | BLE 直接 |
| Android タブレット (キオスク) | 1 | HTTPS |
