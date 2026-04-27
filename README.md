# IoT生活サポートシステム

認知症が進行した高齢者の日常生活をIoTセンサーでさりげなくサポートするシステム。
Raspberry Pi を中枢として、スマートプラグ・開閉センサー・モーションセンサー・カメラを統合し、生活リズムの記録・注意喚起・家族への通知を自動で行う。

## 設計思想

| 原則 | 説明 |
|------|------|
| **監視ではなく記録** | 本人には「自分の記録帳」として提示。監視されていると感じさせない |
| **指摘しない** | 「また食べたでしょ」ではなく「さっき食べましたよ」とやさしく表示 |
| **入力させない** | センサーで自動検知。本人のタブレット操作は最小限 |
| **多層防御** | 気づかせる → 物理阻止 → 家族通知 の3段階 |
| **家族と連携** | LINE双方向 + 家族管理画面で状況確認・操作 |
| **家族操作は見えない** | 編集・解除はおばあちゃんに絶対知られない仕組み |

## 主な機能

### 🍚 食事管理
- スマートプラグ（P110M, Matter）で炊飯器の電力を監視（待機5W / 炊飯1100W）
- 開閉センサー（T110）で冷蔵庫のドア開閉を検知
- カメラ（C220）で顔認識（誰がキッチンにいるか判別）
- 複数センサーの組み合わせで「食事セッション」を自動判定（15分窓）
- 1回目から90分以内の再食事行動 → 炊飯器を自動ロック（電源OFF）
- 「次は何合炊くか」を家族がDB登録、タブレットに表示

### 🛁 お風呂安全監視
- 浴室ドアの開閉センサー（T110）+ 脱衣所のモーションセンサー（T100）
- 入浴開始/終了を自動記録
- 30分間動きがなければ家族にLINE緊急通知

### 💊 お薬管理
- 家族が時刻スケジュールを登録 → タブレットでリマインド
- 家族の証言記録 or 祖母「できた」ボタンで服用ログ
- 未服用なら定期的にLINE通知

### 📱 タブレットUI（本人向け）
- 大きな時計＋日付＋時間帯挨拶（おはようございます／お疲れさまです 等）
- SVGイラストによるお花の成長（達成数で8段階）
- 「わたしのおにわ」（過去14日分の花が並ぶ）
- スタンプカード（起床・お薬・朝食・昼食・お風呂・夕食・就寝）
- 「できた」ボタン（センサー照合付き、未確認なら家族確認に誘導）
- やさしい注意喚起（「さっき食べましたよ」「お薬飲みましたか？」）
- 家族からの伝言表示（青いカード、60分で自動消去）
- **🔊 音声読み上げ**（Web Speech API、ON/OFFトグル）
- **オフライン対応**（Service Worker、Wi-Fi断時もキャッシュ表示）

### 👨‍👩‍👧 家族管理画面
- パスワード認証（外部アクセスはトークン併用）
- ダッシュボード（イベント数・食事回数・電力リアルタイム）
- **🔒 機器管理セクション常駐**（手動ロック / 解除ボタン）
- **📊 7日間アクティビティヒートマップ**（時間帯別密度可視化）
- **📋 家族タスク役割分担**（担当者・時刻設定・LINEリマインド）
- **💊 お薬スケジュール**（朝/昼/夜の時刻設定）
- **🍚 炊飯量ガイド**（次に炊く量を1合単位で指定）
- **💬 タブレット伝言**（プリセット4種＋自由入力、60分有効）
- **⚙️ 詳細設定**（通知ON/OFF・しきい値の家族UI編集）
- かんたん記録（家族が証人として1タップ記録）
- 全イベント一覧（人物フィルタ・編集・削除・日付ナビ）
- リアルタイム電力表示（WebSocket）

### 📨 LINE双方向（Messaging API）
**自動通知**:
- 食事行動検知（2回目以降）
- 炊飯器自動ロック / 解除
- お薬・お風呂・1日のまとめ・週次レポート
- 異常検知（深夜炊飯器・無反応・冷蔵庫開放）
- システム異常時のヘルスチェック通知
- Cloudflare Tunnel URL更新通知

**家族から送れるコマンド**:
| コマンド | 動作 |
|---|---|
| 状況 | 食事・お薬・お風呂・トイレのまとめ |
| 最後の食事 | 直近の食事ラベル＋時刻 |
| タスク | 今日の家族タスク一覧 |
| 済 タスク名 | タスクを完了記録（家族間共有） |
| ロック解除 | 4桁PIN発行 → 数字4桁返信で解除（5分有効・3回失敗で15分ロックアウト） |
| リンク | 最新の公開URL（外出先で繋がらない時用） |
| ヘルプ | コマンド一覧 |

### 🚨 異常検知
独立スクリプト（cron 10分おき）で以下をチェック：
- **深夜炊飯器稼働**（2:00-5:00）→ 徘徊・異常行動の早期発見
- **センサー無反応**（日中4時間以上）→ 倒れている可能性の安否確認
- **冷蔵庫開きっぱなし**（30分以上）→ 食材劣化防止

しきい値は家族UIから動的調整可能。

### 📊 レポート機能
- **1日のまとめLINE**（毎晩22時）：スタンプ達成・食事回数・お花・祖母ボタン検証状況
- **週次レポートLINE**（毎週日曜22時）：食事傾向・薬服用率・お風呂日数・自動ロック発動・気になる傾向の自動抽出
- **ブラウザ版週次レポート**（`/family/weekly-report`）：A4印刷最適化・ChromeでPDF保存可・ケアマネ/医師共有用

### 🌐 外部アクセス
- Cloudflare Tunnel で外出先からもアクセス可能
- トークン認証（タブレット用URLは `?token=xxx`）
- URL更新時にLINEで自動通知 + webhook URL自動再登録
- 「リンク」とLINEに送るだけで最新URL取得可能

### 🛡️ 信頼性・運用機能
- **ヘルスチェック**（5分おき）：iot-web/iot-matter/iot-monitor/Cloudflare Tunnel/ディスク/DB整合性/センサー活動 を独立判定、状態変化時のみ通知
- **DBバックアップ**（毎日3時）：sqlite3.backup() でWAL対応の安全コピー、14日保持
- **DB復元スクリプト**：バックアップ一覧・整合性検証・自動退避付き復元
- **ログローテーション**：日次gzip圧縮、14日保持、エラー集約スクリプト
- **systemd自動起動**：iot-matter / iot-web / iot-monitor

### 📖 家族向け説明書＋操作マニュアル
- `/family/manual` でブラウザ表示（A4印刷でPDF保存可）
- 第Ⅰ部「説明書」（仕組み・原則・プライバシー）
- 第Ⅱ部「操作マニュアル」（LINE・家族画面・タブレット・トラブル対応）
- 全7枚のスクリーンショット埋め込み

## アーキテクチャ

```
[P110M] ──Matter──┐
[T110]  ──H100────┼── monitor.py ── event_bus ── SQLite
[T100]  ──H100────┤       │
[C220]  ──RTSP────┘       ├── bath_monitor    （入浴監視）
                          ├── lock_manager    （自動ロック）
                          ├── face_id         （顔認識）
                          ├── garden          （日次スコア・お花）
                          └── notifier        （LINE通知）
                                  │
                          FastAPI (port 8000)
                          ├── /tablet                （本人用UI）
                          ├── /family                （家族管理）
                          ├── /family/manual         （マニュアル）
                          ├── /family/weekly-report  （週次レポート）
                          ├── /api/*                 （REST API）
                          ├── /line/webhook          （LINE双方向）
                          └── /ws/events             （WebSocket）
                                  │
                          Cloudflare Tunnel
                          └── https://xxx.trycloudflare.com
                                  │
                          LINE Messaging API ⇄ 家族のスマホ
```

### 自動化（cron）
| 頻度 | スクリプト |
|---|---|
| 5分おき | `health_check.py`（ヘルスチェック） |
| 10分おき | `anomaly_check.py`（異常検知） |
| 30分おき(7-22時) | `scheduled_notify.py care_tasks`（家族タスク通知） |
| 9, 12時 | `scheduled_notify.py medicine`（お薬チェック） |
| 18時 | `scheduled_notify.py bath`（お風呂チェック） |
| 22時 | `scheduled_notify.py summary`（1日のまとめ） |
| 22時 日曜 | `weekly_report.py`（週次レポート） |
| 3時 | `backup_db.sh`（DBバックアップ） |
| 4時 | `rotate_logs.sh`（ログローテーション） |
| 起動時 | `start_tunnel.sh`（Cloudflare Tunnel） |

## 技術スタック

| レイヤー | 技術 |
|---|---|
| スマートプラグ制御 | python-matter-server (Matter) / python-kasa (KLAP) |
| バックエンド | Python 3.13, FastAPI, SQLite (WAL) |
| フロントエンド | HTML/CSS/JS, Jinja2, SVG, Web Speech API, Service Worker |
| 顔認識 | face_recognition (dlib) |
| 通知 | LINE Messaging API（Push + Reply + Webhook） |
| 外部公開 | Cloudflare Tunnel (Quick Tunnel) |
| 自動化 | systemd, cron |
| ハードウェア | Raspberry Pi 5 + Tapo製品群 |

## セットアップ

```bash
# 1. リポジトリをクローン
git clone https://github.com/tara0kun/iot-life-support.git
cd iot-life-support

# 2. Python環境構築
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn python-kasa python-matter-server aiohttp \
            requests jinja2 python-multipart itsdangerous websockets \
            face_recognition

# 3. 環境変数を設定
cp .env.example .env
# .env を編集してデバイスIP・パスワード・LINEトークン・チャネルシークレット等を記入

# 4. DB初期化
python -c "from src.db import init_db; init_db()"

# 5. systemdセットアップ
bash systemd/install.sh

# 6. サービス起動
sudo systemctl start iot-matter iot-web iot-monitor

# 7. 顔登録（カメラ前で実行）
python scripts/register_face.py --person-id 1 --name 祖母

# 8. 外部公開（オプション）
bash scripts/start_tunnel.sh

# 9. cron登録（GW_SETUP.md 参照）
crontab -e
```

詳細は **[GW_SETUP.md](GW_SETUP.md)** を参照（出発前チェックリスト・現地手順・cron登録・24時間監視・トラブルシューティング・ロールバック手順を網羅）。

## ブランチ運用

| ブランチ | 用途 | 主な内容 |
|---|---|---|
| `main` | 安定版・本番（祖母宅ラズパイで稼働） | dev からPRマージで更新 |
| `dev` | 開発・テスト用（普段の作業はここ） | 安定したら main にPR |
| `future` | 新機能・実験的コード（Phase 2以降） | 試験的機能・大幅変更 |

**保護設定（main）**: GitHub Settings → Branches で「force push禁止」「削除禁止」を有効化推奨。

### `future` ブランチの実験機能
現在 `future` だけにある追加機能：

- 📱 **祖母タブレット用キオスクAPK** ([android/](https://github.com/tara0kun/iot-life-support/tree/future/android))
  - Kivy + Android WebView で `/tablet` を全画面表示
  - ホームランチャー化により他アプリ起動を防止（Immersive Sticky Mode）
  - 60秒ごとの自動再ロード、KEEP_SCREEN_ON
  - URL設定: ビルド時定数 / `/sdcard/.../url.txt` / GitHub Actions入力
- 🔧 **GitHub Actions: APK自動ビルド** (`.github/workflows/build-apk.yml`)
  - push時にAPKがArtifactsに出力される（Ubuntu 22.04 + JDK 17 + buildozer）
- 📊 **週間サマリー API** (`/api/weekly-summary`)
- 📨 **複数LINE通知先基盤** (`notify_targets` テーブル)
- 🗃️ **データアーカイブスクリプト** (`scripts/archive_old_data.py`)

これらは Phase 2 移行や試験運用が安定後に main へマージ予定。

## プロジェクト構成

```
├── src/
│   ├── db.py                ← DBスキーマ（events / sessions / 設定など全テーブル）
│   ├── event_bus.py         ← イベント記録 + WebSocket配信
│   ├── sessions.py          ← 食事セッション集約
│   ├── monitor.py           ← 全センサー統合監視
│   ├── bath_monitor.py      ← お風呂安全監視
│   ├── garden.py            ← 庭（日次スコア・成長記録）
│   ├── face_id.py           ← 顔認識
│   ├── lock_manager.py      ← 機器ロック/アンロック
│   ├── notifier.py          ← LINE通知（Push + Reply + Webhook URL更新）
│   ├── line_commands.py     ← LINEコマンドディスパッチャ
│   ├── settings.py          ← 動的設定ヘルパ
│   ├── sensors/
│   │   ├── matter_plug.py   ← P110M電力監視 (Matter)
│   │   ├── contact_sensor.py ← T110開閉センサー
│   │   └── camera.py        ← C220カメラ (RTSP)
│   └── web/
│       ├── app.py           ← FastAPIサーバ
│       ├── static/
│       │   ├── manifest.json    ← PWAマニフェスト
│       │   ├── sw.js            ← Service Worker（オフライン対応）
│       │   └── screenshots/     ← マニュアル用画像
│       └── templates/
│           ├── tablet.html       ← 本人用UI
│           ├── family.html       ← 家族管理画面
│           ├── login.html        ← ログイン画面
│           ├── family_manual.html ← 説明書＋操作マニュアル
│           └── weekly_report.html ← 週次レポート（PDF印刷対応）
├── scripts/
│   ├── scheduled_notify.py  ← LINE定期通知（医薬/風呂/まとめ/タスク）
│   ├── weekly_report.py     ← 週次レポートLINE送信
│   ├── anomaly_check.py     ← 異常検知（深夜炊飯器/無反応/冷蔵庫）
│   ├── health_check.py      ← コンポーネント別ヘルスチェック
│   ├── log_summary.py       ← ログ集約（systemd + logs/）
│   ├── rotate_logs.sh       ← ログローテーション
│   ├── backup_db.sh         ← DB安全バックアップ
│   ├── restore_db.py        ← DB復元（一覧・整合性検証付き）
│   ├── start_tunnel.sh      ← Cloudflare Tunnel起動 + webhook URL登録
│   ├── notify_url.sh        ← URL通知（iot-web再起動時）
│   ├── seed_mock_data.py    ← モックデータ投入（5シナリオ）
│   └── register_face.py     ← 顔登録CLI
├── systemd/                 ← systemdサービス定義
├── doc/screenshots/         ← README用スクリーンショット
├── PROGRESS.md              ← 開発進捗記録（現状ダイジェスト含む）
├── GW_SETUP.md              ← GW設置手順書
├── HANDOFF.md               ← 引き継ぎメモ
├── FAMILY_GUIDE.md          ← 家族向けガイド（旧版、現在は /family/manual）
├── .env.example             ← 環境変数テンプレート
└── README.md
```

## スクリーンショット

### タブレット画面（本人向け）

大きな時計と時間帯挨拶、やさしい注意喚起。

<img src="doc/screenshots/tablet_clock_alerts.jpg" width="300">

SVGイラストによるお花の成長と、スタンプカードで1日の達成状況を視覚化。

<img src="doc/screenshots/tablet_flower_stamps.jpg" width="300">

スタンプカード全体と「わたしのおにわ」（過去14日間の成長記録）。

<img src="doc/screenshots/tablet_garden.jpg" width="300">

「きょうのできごと」で1日の活動履歴を色分けアイコンで表示。

<img src="doc/screenshots/tablet_history.jpg" width="300">

### 家族管理画面

ダッシュボード・機器管理・ヒートマップ・かんたん記録。

<img src="doc/screenshots/family_dashboard.jpg" width="300">

イベント一覧。人物フィルタ・ソース別色分け・人物変更ドロップダウン・編集・削除。

<img src="doc/screenshots/family_events.jpg" width="300">

### LINE通知

食事検知アラート、自動ロック、浴室緊急通知、1日のまとめ。

<img src="doc/screenshots/line_notifications.jpg" width="300">

## ライセンス

MIT License
