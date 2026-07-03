# アーキテクチャ (5層モデル)

> `iot-life-support` の内部構造を 5 層に整理したもの。
> 進捗発表 (第2回、2026-06-22) のスライド14で使った「5 層アーキテクチャ」が Pokemon-RL の階層と混同されていた反省から、正しい版を明示する。
>
> **原則**: 各層は下位層のみに依存する。上位層への逆方向依存は禁止。

## 全体図

```
┌───────────────────────────────────────────────────────────────┐
│  Layer 5:  Data                                                │
│  SQLite (WAL) / data/faces/ / data/backup/ / data/private/    │
│  src/db.py                                                     │
└───────────────────────────────────────────────────────────────┘
                        ▲             ▲
                        │             │
┌────────────────────────┴──┐  ┌──────┴───────────────────────┐
│  Layer 4:  UI              │  │  Layer 3:  Notification       │
│  FastAPI + Jinja2 + WS     │  │  LINE Messaging API           │
│  src/web/app.py            │  │  src/notifier.py              │
│  src/web/templates/*.html  │  │  src/line_commands.py         │
│  src/web/camera_stream.py  │  │  scripts/retry_line_outbox.py │
└────────────────────────────┘  └───────────────────────────────┘
                        ▲             ▲
                        └──────┬──────┘
                               │
┌──────────────────────────────┴────────────────────────────────┐
│  Layer 2:  Gateway / Judgment                                  │
│  イベント集約 / セッション化 / 危険判定 / 誤検知抑制             │
│  src/monitor.py           (中央オーケストレータ)                │
│  src/event_bus.py         (asyncio pub/sub)                    │
│  src/bath_detector.py     (湿度+ドア+モーション統合)             │
│  src/sessions.py          (時間ギャップでクラスタリング)          │
│  src/event_summarizer.py  (集計→通知文面生成)                   │
│  src/face_id.py           (dlib face_recognition)              │
│  src/memory_profiler.py   (運用診断)                            │
└───────────────────────────────────────────────────────────────┘
                               ▲
                               │
┌──────────────────────────────┴────────────────────────────────┐
│  Layer 1:  Sensor / Input                                      │
│  物理センサとの通信、生イベント発火                              │
│  src/sensors/camera.py         (Tapo C220 RTSP + 顔認識)        │
│  src/sensors/contact_sensor.py (T110開閉/T100モーション経由H100) │
│  src/sensors/matter_plug.py    (P110M via matter-server)       │
│  src/sensors/switchbot_meter.py(SwitchBot 温湿度計 BLE直接)      │
│  src/sensors/hub_discovery.py  (H100 動的IP検出、DHCP対策)       │
└───────────────────────────────────────────────────────────────┘
                               ▲
                               │
                      ┌────────┴────────┐
                      │  Physical World  │
                      │   祖母宅の実機   │
                      └─────────────────┘
```

## 各層の責務

### Layer 1: Sensor / Input (`src/sensors/`)

**責務**: 物理センサとの通信を抽象化し、統一されたイベントを Layer 2 に流す

| ファイル | 対象 | 通信方式 |
|---|---|---|
| `camera.py` | Tapo C220 | RTSP (2K 映像) + face_recognition |
| `contact_sensor.py` | T110×4 + T100 | H100 hub 経由 (python-kasa) |
| `matter_plug.py` | P110M (炊飯器プラグ) | Matter over WiFi (python-matter-server) |
| `switchbot_meter.py` | 防水温湿度計 W3400010 | BLE Manufacturer Data 直読 (bleak) |
| `hub_discovery.py` | H100 の IP 探索 | broadcast + kasa Discover |

**設計原則**:
- 各センサモジュールは他モジュールを知らない
- Layer 2 のコールバックを受け取って発火するだけ
- 再接続・IP 変動対応はこの層内で完結

### Layer 2: Gateway / Judgment (`src/monitor.py` + 補助)

**責務**: Layer 1 からの生イベントを集約・意味づけし、通知や状態変化を判定

| ファイル | 役割 |
|---|---|
| `monitor.py` | 中央オーケストレータ、全 Layer 1 タスクを起動 |
| `event_bus.py` | asyncio pub/sub (Layer 1 → Layer 2 → 他) |
| `bath_detector.py` | 湿度+ドア+モーションを統合して入浴候補を検知 |
| `sessions.py` | 時間ギャップでイベントを食事セッションにクラスタリング |
| `event_summarizer.py` | 集計から「何が起こったか」の家族向け文面を生成 |
| `face_id.py` | dlib で顔識別、passive 顔学習 (未識別を候補保存) |

**判定ロジックの例**:
- 食事セッション: 15分ギャップでイベントを束ねる、高頻度ソースは除外
- 入浴: 湿度上昇 + ドア開 + モーション のうち 2/3 で候補化
- 危険信号: トイレ長時間 (5分) / 浴室30分静止 / 冷蔵庫30分開放

### Layer 3: Notification (`src/notifier.py` + LINE 双方向)

**責務**: 家族への通知と、家族からの応答受付

| ファイル | 役割 |
|---|---|
| `notifier.py` | LINE Push / Quick Reply / broadcast、outbox キューイング |
| `line_commands.py` | 家族からの LINE コマンド (メニュー・フィードバック等) 処理 |
| `scripts/retry_line_outbox.py` | ネット断時の outbox 再送 (cron 毎分) |

**双方向設計**:
- Push: 危険信号 / セッション確認 / 定時リマインド
- Pull: 家族が LINE で送るコマンド (「メニュー」「意見」「登録」等) を webhook 受信
- Outbox: ネット断時に失敗した Push を JSON Lines に積み、復旧時に自動再送

### Layer 4: UI (`src/web/`)

**責務**: 祖母タブレット + 家族管理画面の Web UI 配信

| ファイル | 役割 |
|---|---|
| `web/app.py` | FastAPI エントリポイント、REST + WebSocket |
| `web/templates/tablet.html` | 祖母タブレット (キオスクモード) |
| `web/templates/family.html` | 家族管理画面 (未対応通知スタック) |
| `web/templates/face_learning.html` | 遠隔顔ラベル付けUI |
| `web/camera_stream.py` | MJPEG ライブ配信 (Tapo C220 stream2) |

**外部公開**: Tailscale Funnel が `localhost:8000` を `https://tara0.taile9fa63.ts.net` にプロキシ (6/3 以降、永久固定URL)

### Layer 5: Data (`src/db.py` + `data/`)

**責務**: 永続化と復元性

- `data/iot.db` — SQLite (WAL モード)、40+ MB、45万件のイベント
- `data/faces/known/` `data/faces/candidates/` `data/faces/encodings.json` — 顔認識データ
- `data/backup/iot_YYYYMMDD.db` — 日次 DB バックアップ (cron 03:00, 14日保持)
- `data/private/system_backups/` — 週次システムスナップショット (7/1 新規)
- `data/line_outbox.jsonl` — LINE 送信失敗の再送キュー

## 直交する運用基盤

Layer 1〜5 とは独立して稼働する運用面のコンポーネント:

### systemd (3 unit)

- `iot-monitor.service` — Layer 1+2 (センサ+判定) 常駐
- `iot-web.service` — Layer 3+4 (通知+UI) 常駐 (uvicorn)
- `iot-matter.service` — python-matter-server 常駐 (Matter 通信の SPOF 化を防ぐ)

### cron (10 job)

| スケジュール | ジョブ | 目的 |
|---|---|---|
| `0 9,12 * * *` | 薬リマインド | Push |
| `0 18 * * *` | 入浴リマインド | Push |
| `0 22 * * *` | 日次サマリ | Push |
| `0 3 * * *` | DB バックアップ + 14日削除 | Data |
| `*/5 * * * *` | health_check | 監視 |
| `*/5 * * * *` | recheck_pending | Notification |
| `*/10 * * * *` | anomaly_check | Judgment |
| `0 22 * * 0` | weekly_report | Notification |
| `0 4 * * *` | rotate_logs | Ops |
| `* * * * *` | retry_line_outbox | Notification 保険 |
| `0 4 * * 0` | system_snapshot | Data 保険 (7/1新規) |

### 外部連携

- **LINE Messaging API** — Push/Reply/Webhook/broadcast、月200通制限 (CRITICAL のみ全員)
- **Tailscale Funnel** — 外部 HTTPS 公開 (6/3〜、永久固定 URL)
- **Cloudflare Quick Tunnel** — 6/3 に廃止
- **matter-server** — localhost:5580 に別プロセスで常駐

## 「5層」の意義

進捗発表 (第2回) では階層の意味が不明瞭なまま提示してしまった。改めて意義を明示:

1. **障害の局所化**: 問題が起きた層を特定できる (センサ層 = 物理、判定層 = ロジック、通知層 = 外部API、UI層 = ブラウザ、データ層 = SQLite)
2. **交換可能性**: 各層は他層を知らないので、Tapo → SwitchBot 等の交換が影響最小
3. **テスト戦略**: 層単位で mock 化 (Layer 1 を fake イベントに差し替えて Layer 2 単体テスト等)
4. **恒久対策の位置決め**: `hub_discovery.py` は Layer 1 内で解決、`retry_line_outbox.py` は Layer 3 の保険… というように「どの層で守るか」が明確になる
