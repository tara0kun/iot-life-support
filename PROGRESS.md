# 認知症祖母IoTサポート プロジェクト 進捗記録

最終更新: 2026-04-20

---

## 🎯 プロジェクトの目的

認知症の祖母の日常生活をIoTでサポートする。設計原則：
- 祖母に「監視されている」と気づかせない
- 直接指摘・命令せず、自分の記録帳として提示する
- 機械の不調に見せて物理的に阻止する
- **家族の操作（編集・バイパス）は祖母に絶対知られない**

解決したい3問題：
- 🍚 食事を何度も摂ってしまう（**最優先**）
- 🚪 訪問販売・勧誘への対応
- 🚿 入浴時に頭を洗わない

追加要件（4/16 家族フィードバック）：
- 🚽 トイレの回数（大/小）も記録

---

## ✅ 2026-04-15 に行ったこと

### 環境構築
- [x] プロジェクトディレクトリ `/home/tara0/IoT/` 作成
- [x] Python venv、python-kasa、FastAPI等インストール
- [x] `.env` / `.gitignore` / `.env.example` 作成

### 初期ファイル作成
- `scripts/discover.py` — Tapoデバイス探索
- `src/db.py` — SQLiteスキーマ
- `src/sessions.py` — 食事セッション集約ロジック

### 設計で合意
1. 「食事行動の回数」を検知（冷蔵庫・炊飯器・IH統合）
2. 検知方式：カメラ＋センサーのハイブリッド
3. 多層防御モデル（気づかせる→物理阻止→家族通知→環境設計）
4. 案A＋B統合（宣言型アンロック＋顔認識ゲート）
5. 祖母UI（`/tablet`）と家族UI（`/family`）の完全分離
6. 全家族メンバーの個別行動記録

### 資料作成
- `proposal.pptx` — 家族会議プレゼン用（14スライド）
- `plan.docx` — 企画書（全11章）

---

## ✅ 2026-04-16 に行ったこと

### 家族フィードバック反映
家族から以下の指摘を受け、設計方針を転換：
1. **祖母がタブレット操作できるか不明** → 入力は極力不要に、センサー自動検知メイン
2. **時間軸の混乱** → タブレットを「生活リズム時計」に（今何時、次は何の時間）
3. **能動的な入力は継続しない** → 「入力させる」→「勝手に記録される」に方針転換
4. **トイレ記録も必要** → ドアセンサー＋任意の物理ボタン
5. **ゲーム感覚** → スタンプカード＋お花の成長＋連続記録（ゲーミフィケーション）

### 機器調達・セットアップ
購入・到着した機材：
| 機材 | IP | 接続方法 | 状態 |
|---|---|---|---|
| **Tapo P110M** × 2 | 192.168.x.x | **Matter** (python-matter-server) | ✅ **電力計測OK** |
| **Tapo C220** | 192.168.x.x | RTSP | ✅ 映像取得OK |
| **Tapo H100**（ハブ） | 192.168.x.x | python-kasa (KLAP) | ✅ 接続OK |
| **Tapo T110** × 3 | H100経由 | python-kasa | ✅ 開閉センサー認識OK |
| **Tapo T100** × 1 | H100経由 | python-kasa | セットアップ待ち |
| Tapo P105（既存） | 192.168.x.x | python-kasa | 補助用 |

#### ⚠️ P110M (Matter版) の重要な技術情報
- P110Mは**TPAP暗号化**を使用しており、python-kasa/tapoライブラリでは直接接続不可
- **python-matter-server** 経由でMatterプロトコルを使い接続に成功
- Matterサーバ: `matter-server --storage-path /home/tara0/IoT/data/matter --port 5580 --bluetooth-adapter 0`
- Matterサーバ起動には `/data/` ディレクトリが必要（`sudo mkdir -p /data && sudo chown tara0:tara0 /data`）
- WebSocket API: `ws://localhost:5580/ws`
- 電力値: `attrs["1/144/8"]` (mW単位 → ÷1000でW)
- ON/OFF: `attrs["1/6/0"]`
- ペアリングコード: Tapoアプリ → デバイス設定 → Matter用設定コード（15分で期限切れ）

#### C220 カメラの技術情報
- RTSP: `rtsp://{camera_user}:{camera_pass}@192.168.x.x:554/stream2`
- カメラアカウントはTapoアカウントとは別（カメラ設定→高度な設定→カメラアカウント）
- pytapoでのPTZ制御は認証エラー（今後対応）
- stream1=2K高画質、stream2=640x360低画質

### システムベース構築
以下のモジュールを実装し、全体パイプラインが動作する状態に：

| モジュール | ファイル | 役割 |
|---|---|---|
| DB | `src/db.py` | SQLiteスキーマ、persons/events/sessions/users/edit_log |
| イベントバス | `src/event_bus.py` | イベント記録＋WebSocketリアルタイム配信 |
| セッション集約 | `src/sessions.py` | 食事セッション判定（15分ギャップ、ソース多様性） |
| P110M電力監視 | `src/sensors/matter_plug.py` | Matter WebSocket経由で電力取得 |
| T110開閉センサー | `src/sensors/contact_sensor.py` | H100ハブ経由で開閉状態監視 |
| C220カメラ | `src/sensors/camera.py` | RTSP映像取得＋HOG人物検知＋Haar顔検知 |
| 統合監視ループ | `src/monitor.py` | 全センサー→イベントバス→DB→セッション集約 |
| Webサーバ | `src/web/app.py` | FastAPI (tablet/family 2経路) |
| 祖母タブレットUI | `src/web/templates/tablet.html` | 時計＋記録帳＋スタンプカード＋次の予定 |
| 家族管理画面 | `src/web/templates/family.html` | 全員イベント一覧＋人物フィルタ＋リアルタイム電力 |

#### アーキテクチャ
```
[P110M] ──Matter──┐
[T110]  ──H100────┼── src/monitor.py ── event_bus ── SQLite (data/iot.db)
[C220]  ──RTSP────┘         │
                            ▼
                    FastAPI (port 8000)
                    ├── /tablet  (祖母用: 読み取り専用)
                    ├── /family  (家族用: 全員閲覧＋編集)
                    ├── /api/*   (REST API)
                    └── /ws/events (WebSocket リアルタイム)
```

#### 動作確認済み
- [x] DB初期化 (祖母/母/祖父のシードデータ)
- [x] P110M電力読み取り (送風機: 待機0.7W → 稼働3.1W)
- [x] C220映像キャプチャ (640x360)
- [x] T110開閉センサー認識 (H100子デバイスとして)
- [x] FastAPIサーバ起動 (tablet/family 両方200 OK)

---

## ✅ 2026-04-17 に行ったこと

### 家族合意事項の確定
| 項目 | 決定 |
|---|---|
| Phase 1スコープ | **食事問題のみ** |
| 祖母への説明 | **「記録帳」として伝える** |
| 祖父の同意 | **OK** |
| 運用担当 | **自分（孫）＋母** |
| 投入時期 | **ゴールデンウィーク中**（4/29〜5/6） |
| 中断基準 | 記録しなくなる、反発、機器破壊、ずるをし始めたら |
| 予算 | 試験的に効果を確認してから追加投資を検討 |

### 統合テスト
- [x] monitor.py 全センサー同時起動テスト
  - P110M（Matter）: ✅ 電力取得OK
  - C220（RTSP）: ✅ 人物検知OK
  - H100/T110: ❌ ハブ電源未投入のためスキップ（リトライ処理追加済み）
- [x] monitor.pyのバグ修正
  - `_running`/`_running_state`フラグの混在修正 → `_active`に統一
  - power_readingの毎回DB書き込み → しきい値変化時のみに修正
  - カメラの連続検知30秒デバウンス追加
  - H100接続失敗時のリトライ処理（5回、クラッシュ防止）
  - `asyncio.gather`に`return_exceptions=True`追加

### systemd自動起動
- [x] サービスファイル作成 (`systemd/iot-matter.service`, `iot-web.service`, `iot-monitor.service`)
- [x] インストールスクリプト (`systemd/install.sh`)
- [x] systemdインストール＋有効化完了
- [x] `iot-matter` + `iot-web` をsystemdで起動確認（active）
- ラズパイ再起動後も自動で立ち上がる

### 家族UI認証
- [x] SessionMiddleware によるパスワード認証
- [x] `/family/login` ログイン画面
- [x] `/family` 未認証時はログイン画面へリダイレクト
- [x] `/family/logout` ログアウト
- [x] `/api/*` エンドポイントも認証必須化
- [x] デフォルトパスワード: `[.envに記載]`（.envで変更可）

### イベント編集API
- [x] `POST /api/events/{id}/edit` — person_idを修正
- [x] edit_logに監査ログ自動記録
- [x] original_person_id保存（元の値を追跡）

### 顔認識モジュール
- [x] `face_recognition` + `dlib` インストール（ラズパイ5でビルド成功）
- [x] `src/face_id.py` — 顔登録・識別クラス（FaceIdentifier）
  - 顔データはJSON + 切り抜き画像で `data/faces/` に保存
  - tolerance=0.5 で照合
- [x] `scripts/register_face.py` — カメラから顔を登録するCLIツール

### LINE通知モジュール
- [x] `src/notifier.py` — LINE Messaging API連携
  - `notify_meal_alert()` — 食事行動検知時の家族通知
  - `notify_device_locked()` — 機器ロック時の通知
  - .envのトークン設定待ち（LINE Developers登録が必要）

### ロック/アンロックモジュール
- [x] `src/lock_manager.py` — Matter経由のP110M ON/OFF制御
  - `lock_device()` — 電源OFF＋DBにロック状態記録
  - `unlock_device()` — 電源ON＋DB更新
  - `should_warn_recent_meal()` — 直近90分以内の食事チェック
  - `auto_lock_after_meal()` — 食事後自動ロック＋2時間後自動アンロック

### monitor.py ↔ ロック＋通知の統合
- [x] セッション集約時に祖母の食事回数を追跡
- [x] 食事2回目以降 ＋ 直近90分以内 → 炊飯器自動ロック → LINE通知
- [x] 食事3回目以降 → 家族にLINE通知（アラート）
- [x] モックデータ（シナリオ2: 食べ過ぎ）で統合ロジック検証済み

### 家族UIの改善
- [x] 各イベントに人物変更ドロップダウン追加（即保存＋✓表示）
- [x] ヘッダーにログアウトボタン追加
- [x] 「祖母の食事回数（今日）」ステータスカード追加（3回以上で赤色警告）
- [x] usersテーブルにadminユーザーをシード（FK制約問題を解決）
- [x] WebSocketライブラリ（websockets）インストール → リアルタイム更新正常化
- [x] systemd TimeoutStopSec=5 追加（WebSocket接続によるシャットダウン遅延解消）

### セッション集約ロジックの修正
- [x] datetime文字列→datetimeオブジェクトの変換処理追加（`_to_dt()`）
- [x] セッション開始時刻をカメラ検知→食事関連イベント基準に変更
- [x] 同ラベルのセッション重複排除（2回目の「朝食」→「間食」に変更）
- [x] `sessions_today()`のクエリパラメータ型修正

### モックデータ＋試運転
- [x] `scripts/seed_mock_data.py` — 3シナリオ
  - シナリオ1: 普通の1日（朝食・昼食・夕食）
  - シナリオ2: 食べ過ぎの日（朝食後30分で再食事行動）
  - シナリオ3: トイレ・入浴含む1日
- [x] タブレットUI表示確認（スタンプ、さいごにたべたのは、次の予定）
- [x] 家族UI表示確認（イベント一覧、人物フィルタ、編集）

### 祖母宅の事前調査
母からの写真で判明：
- [x] **炊飯器**: ZOJIRUSHI NW-VC10（プラグ式）→ P110M制御可、しきい値100W
- [x] **IHコンロ**: HITACHI HT-330S（ビルトイン）→ P110M不可、Phase 1対象外
- [x] **冷蔵庫**: 6ドア観音開き → T110でドア開閉検知可
- [x] **Wi-Fi**: I-O DATA [ルーター型番], SSID=`[.envに記載]`
- [x] **カメラ設置候補**: 壁時計の横（キッチン全体を見渡せる高い位置）
- [x] Wi-Fi事前登録スクリプト (`scripts/setup_grandma_wifi.sh`) 作成済
- [x] mDNS (`[hostname].local`) 既に有効 → IP変更時も `http://[hostname].local:8000/tablet` でアクセス可

### PWA化
- [x] `manifest.json` + アイコン生成
- [x] tablet.htmlにPWAヘッダー追加
- [x] 静的ファイル配信（`/static/`）設定
- Androidタブレットで「ホーム画面に追加」→ アプリと同じ見た目で起動
- APKビルド用のbuildozerプロジェクトも `android/` に用意済み

---

## ✅ 2026-04-20 に行ったこと

### LINE通知設定
- [x] LINE Developersでチャネル作成、トークン発行
- [x] `.env` にトークン＋ユーザーID設定
- [x] テスト通知送信成功
- LINE公式アカウント: [LINE公式アカウントID]

### コードのバグ修正
- [x] `matter_plug.py`: `_active` クラス変数 → インスタンス変数に修正（複数台対応）
- [x] `monitor.py`: `notify_*` を `asyncio.to_thread` でラップ（非同期ブロッキング防止）
- [x] `data/faces/` ディレクトリ作成
- [x] `sessions.py`: `power_off` 単独イベントを食事セッションと認めない修正（間食の誤検知防止）

### お風呂監視モジュール（新規）
- [x] `src/bath_monitor.py` — T110ドアセンサー＋T100モーションで入浴を検知
- [x] 30分間動きなし → 家族にLINE緊急通知
- [x] ドア開閉で入浴開始/終了を自動記録
- [x] `monitor.py` に統合（BathMonitor初期化、コールバック接続）
- [x] T110エイリアス名で浴室ドア/冷蔵庫を自動判別

### お花の成長ゲーミフィケーション
- [x] SVGイラスト全8段階（種→芽→茎→つぼみ→小花→きれいな花→大きな花→満開）
- [x] CSSアニメーション（ゆらゆら揺れる花）
- [x] 絵文字を一切使わないSVGベースのデザイン

### 庭（ガーデン）機能
- [x] `daily_scores` テーブル追加（日ごとの達成記録）
- [x] `src/garden.py` — 日次スコア保存＋過去14日分の庭データ取得
- [x] タブレットに「わたしのおにわ」セクション追加（過去14日の花が並ぶ）
- [x] タブレットアクセス時にスコア自動保存

### 生活リズムガイド
- [x] 「いまは おひるごはんの じかん」のような時間帯ガイド表示
- [x] 現在の時間帯に対応するスタンプが点滅（current フラグ）

### 家族の「かんたん記録」
- [x] `POST /api/quick-record` — 家族が証人として祖母の行動を1タップ記録
- [x] 起床・お薬・お風呂・トイレ・おやつ・就寝の6種
- [x] 証人情報（誰が記録したか）を `raw_meta` にJSON保存
- [x] タブレットのスタンプに即反映

### 注意喚起システム
- [x] タブレットUI:
  - さっき食べた（90分以内）→「さっき たべましたよ」
  - 食事3回以上 →「きょうは よく たべましたね」
  - お薬未服用（9時〜）→「おくすり のみましたか？」（点滅）
  - お風呂未入浴（17時〜）→「おふろ はいりましたか？」
- [x] 家族UI: ロック中バナー＋解除ボタン

### ロック手動解除
- [x] `POST /api/unlock` — 家族が炊飯器ロックを即解除
- [x] 確認ダイアログ付き
- [x] `events` に `family_override` として監査ログ記録

### セッション履歴UIの改善
- [x] 各活動ごとに色・形の異なるSVGアイコン（9種類）
- [x] 左ボーダー色で一目で区別可能に

### 家族UI改善
- [x] タブレットUIと統一されたデザイン
- [x] Noto Sans JP フォント
- [x] モバイル対応レイアウト

### その他
- [x] `GW_SETUP.md` — GW設置手順書（全11ステップ）作成
- [x] モックデータにシナリオ4（充実した1日）追加
- [x] `src/web/static/flower_preview.html` — 花の成長段階プレビューページ

### 外部アクセス（Cloudflare Tunnel）
- [x] `cloudflared` インストール（ARM64、sudoなし）
- [x] Quick Tunnel で公開URL発行
- [x] `scripts/start_tunnel.sh` — トンネル起動＋公開URLをLINEに自動通知
- [x] トップページ `/` → `/tablet` リダイレクト追加

### セキュリティ強化
- [x] タブレット画面にトークン認証追加（外部アクセスのみ）
  - ローカル（192.168.*, 127.0.0.1）はフリーアクセス
  - 外部は `?token=xxx` またはセッション認証が必要
  - トークンは `.env` の `TABLET_TOKEN` で管理
- [x] トンネルURL通知にトークン付きURLを含める

### WebSocket外部アクセス対応
- [x] `ws://` → プロトコル自動判定（`wss://` / `ws://`）に修正
- [x] tablet.html, family.html 両方修正

### LINE定期通知
- [x] `scripts/scheduled_notify.py` — 3種類の定期通知
  - `medicine`: お薬未服用チェック（9時・12時）
  - `bath`: お風呂未入浴チェック（18時）
  - `summary`: 1日のまとめ（22時）— スコア・花の種類・達成/未達成一覧
- [x] crontab に全スケジュール登録済み

### 自動化（crontab）
- [x] `@reboot` — トンネル自動起動（起動30秒後）
- [x] 毎日3時 — DBバックアップ（14日分保持、`data/backup/`）
- [x] 5分おき — ヘルスチェック（`scripts/health_check.sh`）
  - サーバダウン時 → LINE通知
  - 復旧時 → 復旧通知
  - 連続通知防止（フラグファイル方式）

### タブレット最適化
- [x] WakeLock API でスリープ防止
- [x] 5分ごと自動リロード（データ鮮度維持）
- [x] スタンプ: スマホでは4列×2行に折り返し（7列→4列レスポンシブ）

### READMEスクリーンショット追加
- [x] 実機スクリーンショット7枚を `doc/screenshots/` に整理・配置
  - `tablet_clock_alerts.jpg` — 時計＋時間帯ガイド＋注意喚起
  - `tablet_flower_stamps.jpg` — お花の成長＋スタンプカード
  - `tablet_garden.jpg` — スタンプ全体＋わたしのおにわ
  - `tablet_history.jpg` — きょうのできごと一覧
  - `family_dashboard.jpg` — ダッシュボード＋かんたん記録ボタン
  - `family_events.jpg` — イベント一覧テーブル
  - `line_notifications.jpg` — LINE通知（食事検知・ロック・緊急・まとめ）
- [x] `README.md` にスクリーンショットを `<img>` タグで埋め込み（幅300px）
- [x] `.gitignore` に `doc/screenshots/` の例外追加（`doc/*` + `!doc/screenshots/`）

### 家族UI改善（誤タップ防止）
- [x] 「かんたん記録」ボタンに確認ダイアログ追加（`confirm()` で「〇〇を記録しますか？」）
- 誤タップによる意図しない記録を防止

### Git運用体制の構築
- [x] GitHub CLI (`gh`) インストール・認証
- [x] `master` → `main` にリネーム（GitHubデフォルトブランチも変更）
- [x] 旧 `master` ブランチ削除
- [x] ブランチ戦略を策定・作成:

| ブランチ | 用途 | 運用ルール |
|---------|------|-----------|
| **main** | 安定版・本番 | 祖母宅ラズパイはこのブランチを使用。テスト済みのコードのみマージ |
| **dev** | 開発中・テスト中 | 普段の開発作業はここで行う。安定したら `main` にマージ |
| **future** | 新機能・今後の進展 | Phase 2以降の新機能開発。実装が安定したら `dev` にマージ |

#### ブランチ運用フロー
```
future（新機能開発）
  ↓ 実装完了・テスト通過
dev（開発・テスト）
  ↓ 安定動作確認
main（本番・祖母宅）
```

#### 注意事項
- **main に直接コミットしない** — 必ず `dev` 経由でマージ
- **祖母宅ラズパイは `main` を `git pull`** して更新
- **`future` は実験的なコードも許容** — 壊れても `dev`/`main` に影響しない
- ブランチ切り替え: `git checkout dev`（開発時）、`git checkout main`（本番確認時）

---

## 📋 GW投入までの残タスク

### 🔴 高優先度（GW前に必須）
- [ ] **顔登録テスト** — カメラ前で `python scripts/register_face.py --person-id 1 --name テスト` 実行
- [ ] **H100電源投入 → T110統合テスト**
- [ ] **実際の炊飯器での電力しきい値確認**（自宅の電気ケトル等で代用テスト可）
- [ ] **全センサー統合テスト** — monitor.py で全センサー同時起動 → DB → UI → LINE通知の一連確認

### 🟡 中優先度（GW中に対応可）
- [ ] **P110M 2台目のセットアップ**（IHはビルトインなので別用途検討）
- [ ] **T110 残り2台 + T100 のH100ペアリング**
- [ ] **祖母用タブレット端末の調達**
- [ ] **祖母宅Wi-Fi接続テスト**（`scripts/setup_grandma_wifi.sh` 実行）

### 🟢 低優先度（運用開始後でもOK → `future` ブランチで開発）
- [ ] SwitchBot Lock（冷蔵庫用物理ロック）
- [ ] Tapo D230S1（訪問者問題、Phase 2）
- [ ] カメラのPTZ制御（pytapo認証問題の解決）
- [ ] トイレ記録の詳細化（大/小の区別）
- [ ] 訪問販売・勧誘対応機能

---

## 📁 現在のプロジェクト構成

```
/home/tara0/IoT/
├── PROGRESS.md              ← このファイル
├── GW_SETUP.md              ← GW設置手順書（4/20作成）
├── handoff_prompt.md        ← 初期仕様書
├── proposal.pptx            ← 家族会議プレゼン
├── plan.docx                ← 企画書
├── .env / .env.example      ← 認証情報・IP設定
├── .gitignore
├── venv/                    ← Python環境
├── systemd/
│   ├── iot-matter.service   ← Matterサーバ
│   ├── iot-web.service      ← Webサーバ
│   ├── iot-monitor.service  ← センサー監視
│   └── install.sh           ← インストールスクリプト
├── doc/
│   ├── srs/                 ← 実機スクリーンショット原本（.gitignore対象）
│   └── screenshots/         ← README用スクリーンショット（Git管理）
├── src/
│   ├── __init__.py
│   ├── db.py                ← DBスキーマ＋ヘルパー
│   ├── event_bus.py         ← イベント記録＋WebSocket配信
│   ├── sessions.py          ← 食事セッション集約（お風呂対応）
│   ├── monitor.py           ← 全センサー統合起動（お風呂監視統合）
│   ├── bath_monitor.py      ← お風呂監視（T110+T100、緊急通知）
│   ├── garden.py            ← 庭（日次スコア記録・成長花）
│   ├── face_id.py           ← 顔認識（登録・識別）
│   ├── lock_manager.py      ← 機器ロック/アンロック制御
│   ├── notifier.py          ← LINE通知
│   ├── power_monitor.py     ← 旧: python-kasa電力監視
│   ├── sensors/
│   │   ├── __init__.py
│   │   ├── matter_plug.py   ← P110M電力監視 (Matter)
│   │   ├── contact_sensor.py ← T110開閉センサー (H100)
│   │   └── camera.py        ← C220カメラ (RTSP)
│   └── web/
│       ├── __init__.py
│       ├── app.py           ← FastAPIサーバ（認証付き）
│       └── templates/
│           ├── tablet.html  ← 祖母タブレットUI
│           ├── family.html  ← 家族管理画面
│           └── login.html   ← 家族ログイン画面
├── android/
│   ├── main.py              ← Androidアプリ（WebView）
│   └── buildozer.spec       ← APKビルド設定
├── scripts/
│   ├── discover.py          ← Tapo探索
│   ├── register_face.py     ← 顔登録CLI
│   ├── seed_mock_data.py    ← モックデータ投入（4シナリオ）
│   ├── scheduled_notify.py  ← LINE定期通知（お薬・お風呂・まとめ）
│   ├── start_tunnel.sh      ← Cloudflareトンネル起動＋LINE通知
│   ├── health_check.sh      ← ヘルスチェック（5分おき）
│   ├── setup_grandma_wifi.sh ← 祖母宅Wi-Fi事前登録
│   ├── test_camera.py       ← カメラ接続テスト
│   ├── test_p110m.py        ← P110M接続テスト
│   ├── build_proposal.py    ← パワポ生成
│   └── build_plan_doc.py    ← 企画書生成
├── data/
│   ├── iot.db               ← SQLiteデータベース
│   ├── backup/              ← 日次DBバックアップ（14日保持）
│   ├── matter/              ← Matterサーバストレージ
│   ├── faces/               ← 顔認識データ
│   ├── captures/            ← カメラ検知画像
│   ├── tunnel_url.txt       ← 現在の公開URL
│   └── test_frame.jpg       ← カメラテスト画像
├── cloudflared                ← Cloudflareトンネルバイナリ
└── logs/
    ├── monitor.log
    ├── matter.log
    ├── web.log
    ├── tunnel.log           ← トンネルログ
    ├── cron.log             ← 定期通知ログ
    └── health.log           ← ヘルスチェックログ
```

---

## 🔧 起動手順

### 本番（systemd管理）
```bash
# サービス起動
sudo systemctl start iot-matter iot-web iot-monitor

# サービス停止
sudo systemctl stop iot-matter iot-web iot-monitor

# 状態確認
sudo systemctl status iot-matter iot-web iot-monitor

# ログ確認
sudo journalctl -u iot-matter -f
sudo journalctl -u iot-web -f
```

### 開発時（手動起動）
```bash
source venv/bin/activate

# Matterサーバ（systemdで動作中なら不要）
nohup matter-server --storage-path /home/tara0/IoT/data/matter \
  --port 5580 --bluetooth-adapter 0 > logs/matter.log 2>&1 &

# Webサーバ（--reloadで開発）
uvicorn src.web.app:app --host 0.0.0.0 --port 8000 --reload

# センサー統合監視
python -m src.monitor

# 顔登録
python scripts/register_face.py --person-id 1 --name 祖母
```

### アクセスURL
#### ローカル（mDNS対応: IPが変わっても使える）
- 祖母タブレット: `http://[hostname].local:8000/tablet`
- 家族管理画面:   `http://[hostname].local:8000/family` （パスワード: [.envに記載]）
- API:           `http://[hostname].local:8000/api/events`

#### 外部アクセス（Cloudflare Tunnel）
- `bash scripts/start_tunnel.sh` で公開URL発行 → LINEに自動通知
- タブレットはトークン付きURL（LINEで届く）でアクセス
- 家族画面はパスワード認証（[.envに記載]）
- ラズパイ再起動時は `@reboot` cronで自動起動

### モックデータ投入（試運転用）
```bash
source venv/bin/activate
python scripts/seed_mock_data.py --clear --scenario 1  # 普通の1日
python scripts/seed_mock_data.py --clear --scenario 2  # 食べ過ぎの日
python scripts/seed_mock_data.py --clear --scenario 3  # トイレ・入浴含む
python scripts/seed_mock_data.py --clear --scenario 4  # 充実した1日
```

---

## 💡 覚えておくべきこと

1. **P110MはMatter経由でしか接続できない**（TPAP暗号化、python-kasa/tapo非対応）
2. **Matterサーバには`/data/`ディレクトリが必要**（sudo作成済み）
3. **C220カメラアカウントはTapoアカウントと別**（.envのCAMERA_USERNAME/PASSWORD）
4. **Wi-Fiは2.4GHz**（`[自宅Wi-Fi SSID]`）。全Tapo機器が同じSSID
5. **祖母UIと家族UIはDOMレベルで分離**。編集機能は家族UIにのみ存在
6. **家族フィードバック**: タブレット入力は期待できない→センサー自動検知メイン＋ゲーミフィケーション
7. **家族パスワード**: [.envに記載]（.envのFAMILY_PASSWORDで変更可）
8. **face_recognitionはsetuptools<70が必要**（pkg_resources依存）
9. **GW投入目標: 2026-04-29〜05-06**
10. **IHコンロはビルトイン型**（HITACHI HT-330S）→ P110M不可、Phase 1対象外
11. **祖母宅Wi-Fi**: SSID=`[.envに記載]`, PW=`[.envに記載]`（.envに記録済）
12. **mDNS有効**: `[hostname].local` でラズパイにアクセス可（IP変更に強い）
13. **LINE公式アカウント**: [LINE公式アカウントID]（通知用）
14. **タブレットトークン**: `.env`の`TABLET_TOKEN`（外部アクセス用）
15. **Cloudflare Tunnel**: アカウントなし版（Quick Tunnel）。URLは再起動で変わるがLINEで自動通知
16. **crontab登録済み**: 定期通知(9,12,18,22時)、トンネル自動起動、DBバックアップ(3時)、ヘルスチェック(5分おき)
17. **お風呂で祖母が意識喪失した過去あり** → T110+T100による浴室監視は安全面で重要
18. **浴室ドアのT110エイリアスは「浴室ドア」にリネーム必須**（monitor.pyが名前で判別）
19. **Gitブランチ**: main（本番）/ dev（開発）/ future（新機能）の3ブランチ運用。mainに直接コミット禁止
20. **GitHub CLI**: `gh` インストール済み、HTTPS認証済み。リポジトリ: `tara0kun/iot-life-support`
21. **GitHubデフォルトブランチ**: `main`（旧masterは削除済み）
22. **doc/screenshots/ のみGit管理**（doc/の他ファイルは.gitignore対象）

---

## 📞 次回のプロンプト例

> "PROGRESS.md を読んで続きを進めて。今日は [X] をしたい。"

例：
- 「顔登録をテストしたい」（カメラの前で実行）
- 「H100のハブを復帰させたのでT110のテストをしたい」
- 「全体通しテストをしたい」（全センサー→DB→UI→通知の一連）
- 「GW前の最終チェックをしたい」
- 「futureブランチで新機能を開発したい」（Phase 2機能）

### ブランチ切り替え
```bash
git checkout dev      # 開発作業
git checkout main     # 本番確認
git checkout future   # 新機能開発
```
