# 認知症祖母IoTサポート プロジェクト 進捗記録

最終更新: 2026-05-22

---

## 📌 現状ダイジェスト（最初に読む）

**ステータス**: 遠隔運用フェーズ。モンスタークラスター・時間ズレ通知の根本治癒完了。家族UIの未対応通知一覧で対応する方式が確立
**ブランチ**: `dev` で開発継続（main から **55+ コミット先行**、5/22 まで全コミット保存済）
**未コミット変更**: なし（全機能 git 保存済）

### 🏠 祖母宅セットアップ状況（5/5時点）
- **ラズパイ5**: WiFi化完了（IODATA-2G / 192.168.0.31）、Tailscale 経由 SSH 可能
- **稼働中センサー**: P110M炊飯器 / H100ハブ / T110×4（冷蔵庫/風呂/トイレ/炊飯器蓋）/ T100×1（脱衣所）/ C220カメラ / SwitchBot温湿度計
- **顔認識**: 祖母（30件以上）+ はるか（妹、8件）登録、stream1+upsample=2 で動作。祖父は未登録
- **未設置**: T110（歯ブラシ/シャンプー）、T100（洗面所）、S200B、ドライヤーP110M

### 🆕 5/4-5/8 セッション主な変更（コミット c41a371 / e26f486 / 8f5c0ee / a4d5d48 / 047ffe7）

**インフラ**:
1. 全センサーIP更新（HUB=192.168.0.30、RICE=0.24、CAM=0.29）
2. T100 MotionSensor 対応（contact_sensor.py）
3. SwitchBot W3400010 Manufacturer Data パーサー
4. Pi WiFi化（eth0 → wlan0）
5. タイムゾーンバグ修正（recheck_pending）

**UX/機能**:
6. タブレット2段確認モーダル（押し間違い防止）
7. 家族UI 行動サマリ（event_summarizer.py、複数センサ予測）
8. ライブカメラ映像（家族UIから MJPEG）
9. 食事写真をタブレット側に表示
10. 外食/朝食/昼食/夕食/間食 手動記録ボタン
11. 家族からの伝言の即時自動更新（WebSocket）
12. 7日間ヒートマップ タブ式化
13. 通知文言の自然化（"event #1234"→"炊飯器の動作を…"）

**判定ロジック**:
14. 未確定セッション → LINE確認フロー（confirmed カラム）
15. BathDetector（湿度+ドア+モーション統合判定）
16. ロック確認の事前化（実行前に LINE 質問）
17. 通知の選択肢拡張（了解/対応不要 2択 + 炊飯器 5択）
18. 炊飯器の保温自動分類（蓋閉+学習1件で抑制）
19. 関連通知の自動連動（meal_alert↔device_locked、rice_action 一括クリア）
20. ドライヤー無効時のリマインドループ停止
21. 全家族アクションの broadcast（誰が対応/解除/記録）

**5/5-5/8 追加機能（運用改善・遠隔操作・ガイド整備）**:
22. 誤検知抑制の強化（bath_motion・bathroom_meter・toilet_door を食事判定除外、冷蔵庫単独停止、深夜厳格化）
23. 夜間通知抑制（1〜5時、緊急系のみ通す）
24. トイレ長時間滞在アラート（5分以上で CRITICAL 通知）
25. 学習状況可視化UI
26. 受動的顔学習システム（未識別顔の自動収集 + `/family/face-learning` で遠隔ラベル付け）
27. LINE メニュー機能（「メニュー」コマンド、URI+postback 混在 Quick Reply）
28. 意見ボックス機能（「意見/質問/要望/バグ」コマンド、admin 即時転送）
29. センサ可視化拡張（24h/7d 反応回数、通知応答内訳、候補顔キュー）
30. 判定根拠の通知メッセージ含有（session_confirm にソース別反応回数表示）
31. 人物推定の時間重み付け（直近2分のカメラ識別を高信頼度に）
32. LINE操作ガイド（`/guide/line-operation`）+ ガイドルートの .md 自動除去
33. 炊飯器保温パルス通知の根本治癒（SQL JST/UTC タイムゾーンバグ修正）

### 5/8 時点の登録家族
- person_id=1: 祖母 / 3: 祖父 / 4: ゆきこ / 5: みきこ / 6: まきこ / 7: はるか（妹）

---

## 🚧 残タスク（5/8時点・優先度別）

### 🆕 5/11-5/22 セッションで追加対処

- [x] **トンネル systemd 化** （cloudflared 自動更新でも auto-restart）
- [x] **webhook URL 更新の信頼性向上** （5回×6秒リトライ）
- [x] **再通知廃止と家族UI スタック化** （深夜の時間ズレ通知問題を根本解消）
- [x] **モンスタークラスター根治** （bathroom_meter / bath_motion / camera を集約から除外）
- [x] **古いセッションのLINE通知ガード** （2時間以上前はスキップ、家族UIにのみ表示）
- [x] **ディスク 87GB 解放** （save_detections=False、camera フレームの無駄保存停止）
- [x] **顔学習閾値の柔軟化** （80px→60px + .env 化）
- [x] **時刻バッジ表示** （家族UI で事象時刻を目立つ位置に）
- [x] **過去のモンスター 22件無効化** （confirmed=-1）

### ✅ 運用観察結果（5/19 確認）

- モンスタークラスター: **再発なし**（最大66件、過去5000件超）
- LINE 通知遅延: **1〜21分**（過去11時間超の深夜通知あり）
- サービス連続稼働: 4〜8日無停止
- 受動的顔学習: 候補顔が定期的に追加されている

### ✅ 5/8 までに対処済（運用観察待ち）

- [x] bath_motion のモンスタークラスター対策（`_qualifies_as_session` で除外）
- [x] 炊飯器の保温電圧変化のみで通知発火問題（蓋センサ時間相関 + JSTタイムゾーンバグ修正）
- [x] 家族UI 湿度センサ readings 除外
- [x] 夜1〜5時の通知抑制（緊急系のみ通す）
- [x] 既存 confirmed=0 セッション全件クリーンアップ
- [x] iot-monitor の session_confirm 新コード反映
- [x] トイレ判定すり合わせ（5分以上アラート + 10秒未満除外 + 時間重み付け人物推定）
- [x] 食事行動真偽すり合わせ（蓋開必須化、深夜厳格化）
- [x] 学習状況確認 UI（家族UIに 📚 セクション）
- [x] センサ反応・判定根拠の可視化（24h/7d、通知応答内訳、判定根拠 message 含有）
- [x] LINE リッチメニュー（Quick Reply 方式）+ 意見ボックス
- [x] 使い方ガイド一式（README/タブレット/祖母/家族/LINE操作/リファレンス/FAQ）

### 🟡 残中優先度

- [ ] **祖父さんの顔登録**: 受動収集システムが回り始めたら家族UI で遠隔ラベル付け
- [ ] **カメラ+センサ統合推測**（「ご飯食べてる？」をカメラで確認）

### 🟢 低優先度（大規模・別セッション推奨）

- [ ] **dev → main マージ**（運用安定後、48コミット先行）
- [ ] **S200B ボタン 2台のコード対応**（玄関訪問記録/緊急呼出）
- [ ] **追加 T110/T100**（歯ブラシ/シャンプー/洗面所モーション）
- [ ] **画像付き LINE リッチメニュー**（現状はテキスト Quick Reply、より美しく）

### 📊 運用観察フェーズ（次セッション以降）

- 受動的顔学習で候補顔がどれくらい溜まるか
- 通知数が適切に減っているか
- 誤検知ゼロを目指す微調整
- 学習状況UIから「不明/他家族」回答が多い通知タイプを特定→改善

---

## 📚 5/2-5/3 で実装した主な機能（過去履歴）

1. **家族の LINE 自己登録**（「登録 ゆきこ」のような自由名で family_line_users に追加）
2. **未確定セッションの人物確認**（Quick Reply で家族が割当、近接統合）
3. **全アラートの actionable 化**（「✓ 確認した」ボタン → 全員ブロードキャスト → 30分応答無しで再通知 → タイムアウト）
4. **炊飯器の曖昧電力分類学習**（rice_classifications テーブルに家族訂正を蓄積、3件以上類似なら自動分類）
5. **食事検知の厳格化**（蓋センサー稼働時は炊飯器単独 power_on を食事と認めない）
6. **ドライヤー P110M による髪洗い検知**（HAIR_DRYER_NODE_ID 設定で有効化）
7. **SwitchBot 防水温湿度計のコード準備**（5/4 で実機接続、Manufacturer Data パーサー実装）

---

## 📜 元記述（2026-04-27 当時）

> **ステータス**: GW投入準備中（**残り2日**: 4/29〜5/6 設置）
> **ブランチ**: `main` = `dev`（PR #1でマージ済）／`future` は main をマージ済 + 実験機能搭載
> **最新コミット (main)**: `bb6c85e`（PRマージ）／**future**: `e80283c`（mainマージ＋コンフリクト解消）

### 🔒 ブランチ保護設定（GitHub Settings → Branches で推奨）
- main: **force push禁止 / 削除禁止**（誤操作防止）
- 通知: 「Your main branch isn't protected」警告は force push 禁止/削除禁止で消える

### 動作中のコンポーネント
- ✅ Webサーバ (`iot-web`) — タブレットUI / 家族UI / API / WebSocket
- ✅ Matterサーバ (`iot-matter`) — P110M電力監視・ロック制御の基盤
- ✅ Cloudflare Tunnel — 外部アクセス（URL自動LINE通知）
- ✅ LINE Messaging API — 通知＋webhook双方向（「リンク」「状況」「タスク」「ロック解除」等）
- ⏳ センサー監視 (`iot-monitor`) — H100ハブ電源未投入のため未稼働

### GW前にやり残し（高優先度）
1. **顔登録テスト** — `python scripts/register_face.py --person-id 1 --name 祖母`
2. **H100電源投入 → T110統合テスト**
3. **実炊飯器での電力しきい値確認**
4. **全センサー統合テスト** — monitor.py 通しで DB → UI → LINE通知まで
5. **cron追加**（GW前に有効化推奨）:
   ```cron
   */30 7-22 * * *  cd ~/IoT && venv/bin/python scripts/scheduled_notify.py care_tasks
   0 22 * * 0       cd ~/IoT && venv/bin/python scripts/weekly_report.py
   */10 * * * *     cd ~/IoT && venv/bin/python scripts/anomaly_check.py
   ```

### 直近実装（2026-04-22）
| 機能 | 概要 |
|---|---|
| LINE webhook双方向 | 「リンク」で最新URL自動返信、トンネル起動時にwebhook URL自動登録 |
| LINEコマンド体系 | 状況・最後の食事・タスク・済・ロック解除・ヘルプ |
| ロック解除PIN | 4桁コード・5分有効・3回失敗で15分ロックアウト |
| 機器管理セクション常駐 | 家族UIで手動ロック/解除がいつでも可能 |
| 「状況」情報拡充 | お薬・お風呂・トイレ・家族証言を追加 |
| 家族タスク役割分担 | care_tasks DB＋LINE通知＋家族UI管理 |
| 週次レポート | 7日分の食事・スタンプ・薬・お風呂を集計してLINE送信 |
| タブレット音声読み上げ | Web Speech API、ON/OFFトグル付き |
| 異常検知 | 深夜炊飯器・無反応・冷蔵庫開放を10分おきにチェック |

### 直近実装（2026-04-24）
| 機能 | 概要 |
|---|---|
| GW_SETUP.md大幅拡充 | 出発前チェック・cron登録・24h監視・ロールバック手順 |
| ヘルスチェック強化 | 7コンポーネント別状態・OK→NG/NG→OK変化通知・30分クールダウン |
| 家族デモ用モック | scenario 5: 設置当日に全機能を見せる1日分データ |
| タブレットUI調整 | フォント拡大・ボタン拡大（min 44pxタッチ領域）・自動更新3分 |
| 動的設定機能 | settings DB＋家族UI で通知ON/OFF・しきい値編集 |
| 7日ヒートマップ | 家族UIに過去7日×24時間のイベント密度可視化 |
| 週次レポートPDF | /family/weekly-report をブラウザ表示→Chrome PDF保存 |
| ログローテーション | 日次gzip圧縮・14日保持・エラー集約スクリプト |
| DB復元スクリプト | バックアップ一覧・整合性検証・自動退避付き復元 |
| Service Worker | タブレットのオフライン対応・接続断バナー・復活時自動更新 |

### 残っている改善案（着手判断要）
- **Named Tunnel化**（要Cloudflareアカウント＋独自ドメイン年1,500円）
- **訪問販売対応**（要 Tapo D230S1 ドアベル）
- **服薬自動化**（要 T110＋薬箱）
- **週次レポートのメール送信**（PDFはブラウザ印刷で対応済み）
- **声の感情分析**（倫理議論待ち）
- **CSRF対策・レート制限**（家族のみアクセス前提のため緊急性低）

### `future` ブランチの実験機能（main へマージ前）
- 📱 **タブレット用キオスクAPK**（[android/](android/)） — Kivy + WebView、ホームランチャー化、Immersive Mode、URL設定3経路
- 🔧 **GitHub Actions: APK自動ビルド**（`.github/workflows/build-apk.yml`） — push時にArtifactsへAPK出力
- 📊 **週間サマリー API** (`/api/weekly-summary`)
- 📨 **複数LINE通知先基盤** (`notify_targets` テーブル) — 家族別の通知レベル設定（all/urgent/daily）
- 🗃️ **データアーカイブスクリプト** (`scripts/archive_old_data.py`)
- ⚠️ **エラー時フォールバック**（device_state取得のtry/except）

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

---

## ✅ 2026-04-21 に行ったこと

### 家族フィードバック反映（4/20夜のLINE議論）
家族から以下のフィードバックを受けて対応:

### 花の色バリエーション追加
- [x] 8種類の花の色を追加（桜・チューリップ・すみれ・ひまわり・あじさい・バラ・コスモス・たんぽぽ）
- [x] 日付のハッシュで花の色が一意に決まる仕組み（毎日違う花が咲く）
- [x] 庭の花にも色を反映（data属性 + JS recolorFlower関数）
- [x] 花が咲いた場合（stage 4以上）は花の名前を表示（「桜が咲いた！」）

### 炊飯量ガイド表示
- [x] 食事の時間帯に「ご飯は○合」と表示
- [x] 現在の時間帯ガイドと、次の食事予告の両方に表示
- [x] `.env` で炊飯量をカスタマイズ可能（`RICE_GUIDE_朝食=1合` 等）
- デフォルト: 朝食1合、昼食1合、夕食2合

### 家族UIに日付管理機能追加
- [x] 日付ナビゲーション（前日/翌日ボタン + 日付ピッカー + 今日に戻る）
- [x] `GET /family?date=YYYY-MM-DD` で過去のイベントを閲覧
- [x] `get_events_by_date()` API追加（event_bus.py）

### 家族UIのイベント時刻編集・削除
- [x] イベント時刻をクリックで編集（time input に切り替え）
- [x] 各行に削除ボタン（×）追加、確認ダイアログ付き
- [x] `DELETE /api/events/{id}` API追加（監査ログ記録）
- [x] `POST /api/events/{id}/edit` に `started_at` 更新機能追加

### タブレットUIの漢字化
- [x] ひらがな表記を漢字に変更（祖母は漢字が読める：家族確認済み）
- 変更箇所: 時間帯ガイド、注意喚起、カードラベル、花メッセージ、庭タイトル等

### ソース名の日本語化
- [x] 家族UIのイベント一覧でソース名を日本語表示
  - `ih` → `IH`、`contact_sensor` → `開閉センサー`、`rice_cooker` → `炊飯器`
  - `camera` → `カメラ`、`family_report` → `家族記録` 等

### 「最後に食べたのは」バグ修正
- [x] 就寝・起床などの非食事セッションが「最後に食べたのは」に表示されるバグを修正
- [x] 食事ラベル（朝食・昼食・夕食・間食・おやつ）のみフィルタして表示

### 家族用ガイド作成
- [x] `FAMILY_GUIDE.md` — 家族向けの使い方ガイド（全セクション）
  - アクセス方法、管理画面の使い方、LINE通知、トラブルシューティング

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

### 日付ナビゲーション修正
- [x] `family_view` で常に `get_events_by_date` を使うよう修正（全件表示バグ解消）
- [x] モックデータに `--days N` オプション追加（過去N日分を自動生成）
- [x] モックデータに `--clear-all` オプション追加

### 炊飯量設定
- [x] `rice_guide` テーブルでDB管理
- [x] 食事ごとの3スロット → 「次に炊く量」1つに簡素化
- [x] 家族UIにプリセットボタン（1合/2合/3合）+ 自由入力 + クリア
- [x] タブレット表示:「ご飯は 2合 炊いてね」（設定時のみ、クリアで非表示）
- [x] API追加: `GET/POST /api/rice-guide`, `DELETE /api/rice-guide/{meal}`
- [x] `.env` ハードコードからDB参照に変更
- [x] 家族が未登録の場合、タブレットに炊飯量は表示されない

### 祖母タブレット「できた」ボタン
- [x] スタンプカードの未完了項目（起床/お薬/お風呂/トイレ/就寝）に「できた」ボタン
- [x] ボタン押下時にセンサー記録を照合（`_verify_sensor()`）
  - **センサー確認済み** → スタンプ記録OK、LINE「確認済み」通知
  - **センサー記録なし** → 記録しない、アラート「記録が見つかりません」、LINE「確認してください」通知
  - **センサーなし（お薬/就寝）** → 記録しない、「家族に伝えてください」表示、LINE通知
- [x] `POST /api/tablet-record` API追加（`tablet_report` ソース）
- [x] 家族UIにソース「祖母ボタン」を青紫バッジで表示
- [x] 未検証ボタン押下は `○○_unverified` としてイベント記録（confidence=0.0）

### センサー反応 + ボタン未押下のアラート
- [x] お風呂・トイレのセンサーが反応 + スタンプ未完了 → タブレットに「○○しましたか？ ボタンを押してください」アラート

### 活動時間の指定を削除
- [x] 「朝ごはんの時間」「お風呂の時間」等の時間指定ガイドを削除
- [x] 時間帯の挨拶に変更（おはようございます/良い一日を/お疲れさまです/おやすみなさい）
- [x] 「次は夕ごはん（18:00）」の予告を削除
- [x] スタンプのcurrentフラグ（時間指定の点滅）を削除
- [x] 一般的な時間帯での促し（「朝ごはんは食べましたか？」等）は残す

### 薬スケジュールの家族UI管理
- [x] `medicine_schedule` テーブル追加（timing: 朝/昼/夜, hour: 0-23）
- [x] 家族UIに「お薬スケジュール」設定セクション追加（朝/昼/夜ごとに時刻設定・削除）
- [x] タブレットのリマインドはDBスケジュールに基づく（未設定なら非表示）
- [x] 定期通知スクリプト（scheduled_notify.py）もDB参照に変更

### 家族→タブレット伝言機能
- [x] `family_prompts` テーブル追加（メッセージ、期限、確認済みフラグ）
- [x] 家族UIに「タブレットに伝える」セクション追加
  - プリセット4種（お薬/お風呂/ご飯/電話）+ 自由入力
- [x] タブレットに青いメッセージカードで表示、「わかった」ボタンで非表示
- [x] 60分で自動期限切れ

### iot-web再起動時のURL通知
- [x] `scripts/notify_url.sh` — 現在のトンネルURLをLINEに再通知
- [x] `iot-web.service` に `ExecStartPost` 追加（起動後に自動通知）

### バグ修正
- [x] 家族UIの時刻表示: `2026-` → `07:00`（空白区切りdatetime対応）
- [x] 家族UIのヘッダー: `2026/04/21 15:30` → `4/21（月）15:30`
- [x] 家族メッセージ非表示バグ: datetime形式の不一致（T区切り/空白区切り）を統一
- [x] 祖母「できた」ボタンからトイレを除外（センサー自動検知のみ）

---

## ✅ 2026-04-22 に行ったこと

### LINE webhook双方向化（URL再送信機能）
- [x] `src/notifier.py` に `reply_line_message()` / `update_webhook_url()` 追加
- [x] `src/web/app.py` に `GET/POST /line/webhook` エンドポイント追加
  - X-Line-Signature 署名検証（HMAC-SHA256）
  - 許可送信者リスト `LINE_ALLOWED_SENDERS`（未設定なら `LINE_USER_ID` にフォールバック）
- [x] `scripts/start_tunnel.sh` / `scripts/notify_url.sh` にLINE API `PUT /v2/bot/channel/webhook/endpoint` 呼び出し追加
  - Cloudflare Quick TunnelのURLが変わるたびにwebhook URLを自動登録
- [x] `.env.example` に `LINE_CHANNEL_SECRET` / `LINE_ALLOWED_SENDERS` 追加
- LINE Developersコンソール側で「Webhookの利用」ON + チャネルシークレットを `.env` に設定済み

### LINEコマンド体系の導入
- [x] `src/line_commands.py` 新規作成（ディスパッチャ）
- [x] 「リンク」「URL」「つながらない」「見れない」「アクセス」「開けない」「接続」 → 最新URL返信
- [x] 「状況」「様子」「今日」 → 食事回数・スタンプ達成・最後の食事・ロック状態サマリー
- [x] 「最後の食事」「さっき」 → 直近の食事ラベルと経過時間
- [x] 「ヘルプ」「help」「コマンド」「使い方」 → コマンド一覧
- [x] 「ロック解除」「解除」「アンロック」 → 4桁確認コード発行
- [x] 数字4桁入力 → 確認コード照合 → Matter経由でロック解除実行
- [x] セキュリティ: 確認コード5分有効、3回失敗で15分ロックアウト（プロセス内メモリ管理）
- [x] 優先順位マッチ: ロック解除 > 確認コード > 状況 > 最後の食事 > ヘルプ > リンク

### 家族UIに機器管理セクション常駐化
- [x] 従来は「ロック中のみバナー表示」→ 「常時表示の機器管理カード」に変更
- [x] 状態バッジ（🔒 ロック中 / 🔓 通常）を常時表示
- [x] 状態に応じて「ロック解除」または「手動ロック」ボタンを出し分け
- [x] `POST /api/lock` エンドポイント追加（予防的な手動ロック用）
  - `family_override` + `event_type=lock` として監査ログ記録

### 「状況」コマンドの情報拡充
- [x] 食事回数・最後の食事に加え、お薬服用状況、お風呂時刻、トイレ回数、家族証言の最新3件を表示
- [x] LINE1通で1日の全貌が把握できる状態に

### 家族タスク役割分担機能
- [x] `care_tasks` / `care_task_logs` テーブル追加
- [x] LINE「タスク」「担当」 → 今日のタスク一覧＋完了状況
- [x] LINE「済 タスク名」 → 完了記録（重複防止）
- [x] `scheduled_notify.py` に `care_tasks` モード追加（時刻に担当者へリマインド）
- [x] 家族UI `/family` にタスク管理セクション追加（新規登録・削除）
- [x] `GET/POST /api/care-tasks`, `DELETE /api/care-tasks/{id}` 追加

### 週次レポート自動生成
- [x] `scripts/weekly_report.py` 新規作成
- [x] 過去7日の食事回数（平均・最大・食べ過ぎ日数）、スタンプ達成率、お薬確認率、お風呂日数、自動ロック発動数、家族タスク完了率を集計
- [x] 日別食事数を🍚アイコンでチャート化
- [x] 気になる傾向（お風呂スキップ・薬・スタンプ）を自動抽出
- [x] crontab例: `0 22 * * 0` 毎週日曜22時

### タブレット音声読み上げ (Web Speech API)
- [x] 家族メッセージと注意喚起を自動音声化（日本語・少し遅め）
- [x] ヘッダーに「🔊 音声ON / 🔇 音声OFF」トグル（localStorage永続化）
- [x] ブラウザautoplay制約対応（初回タップで有効化）

### 異常検知
- [x] `scripts/anomaly_check.py` 新規作成（3種類のチェック）
  - 深夜炊飯器稼働 (2:00-5:00に rice_cooker power_on)
  - センサー無反応タイマー (日中7-22時、4時間以上無反応)
  - 冷蔵庫開きっぱなし (open後30分 close無し)
- [x] 日次通知フラグで重複防止（`data/anomaly_flags/<key>_<date>`）
- [x] crontab例: `*/10 * * * *` 10分おき

---

## ✅ 改善点・検討事項の対応状況

### 🔴 優先度高 → 完了
- [x] **「できた」ボタンのクールダウン** — 30分間連打防止
- [x] **`/api/tablet-record` の認証** — ローカル or トークン認証済みのみ

### 🟡 優先度中 → 完了
- [x] **Python 3.12+ datetime非推奨警告の対応** — `detect_types` 削除、文字列で処理
- [x] **セッション集約の整合性** — 手動記録ソースを `aggregate_sessions()` から除外
- [x] **1日のまとめ通知に祖母ボタン検証状況** — 確認済み/未確認の内訳を追加
- [x] **家族UIリアルタイム更新の日付対応** — 今日の画面のみ自動リロード

### 🟢 優先度低 → futureブランチで対応
- [x] **タブレットのオフライン対応** — Service Worker追加（futureブランチ）
- [x] **週間ダッシュボード** — 過去7日の食事回数・スタンプ達成率（futureブランチ）
- [x] **複数LINE通知先** — `notify_targets` テーブル基盤（futureブランチ）
- [x] **古いデータのアーカイブ** — `scripts/archive_old_data.py`（futureブランチ）
- [x] **エラー時フォールバック** — device_state取得のtry/except保護（futureブランチ）

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

## 💡 今後の改善点・追加機能アイデア（2026-04-22 起票）

GW投入後、運用が安定してから着手する候補。優先度と見積工数の目安付き。

### 🟡 中期的（Phase 1運用開始後）

- [x] ~~「状況」コマンドの情報拡充~~ — 2026-04-22 完了
- [x] ~~異常検知の能動通知~~ — 2026-04-22 完了（深夜炊飯器・無反応・冷蔵庫）
- [x] ~~家族間の役割分担通知~~ — 2026-04-22 完了
- [x] ~~週次レポート自動生成~~ — 2026-04-22 完了（LINE送信・PDF化は保留）
- [ ] **Named Tunnel化（URL固定化）**（中: 2h + 年1,500円）
  - Cloudflareアカウント + 独自ドメインでURLを固定
  - webhook URL登録が1度で済む
  - 「リンクが欲しい」機能はバックアップとして残す
- [ ] **週次レポートのPDF化 + メール送信**
  - ケアマネージャー/医師に共有できるフォーマット
  - LINE版は実装済み、PDF化は必要に応じて追加

### 🟢 Phase 2以降（`future` ブランチ候補）

- [ ] **訪問販売・勧誘対応**（Phase 2の本丸）
  - Tapo D230S1 ドアベル + 顔認識で家族/知人/未知を判別
  - 未知の訪問者 → タブレットに「ドアを開けないでください」表示 + LINE通知
- [ ] **服薬管理の自動化**
  - 薬箱にT110開閉センサーを取り付け
  - ボタン押下ではなく物理的な開閉で「飲んだ」を自動記録
- [x] ~~音声読み上げ~~ — 2026-04-22 完了（タブレット搭載、ON/OFFトグル付き）
- [ ] **祖母の声の感情分析（遠大）**
  - タブレットにマイクで「元気？」と話しかけさせる
  - 返答の感情をAI判定、落ち込み傾向が続く → 家族に通知
  - ※プライバシーと倫理の議論が必要
- [ ] **データ可視化（家族UI拡張）**
  - 過去30日の食事パターンヒートマップ
  - 体調変化の相関分析（食事回数 vs お風呂未入浴日 等）

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
23. **炊飯量ガイドはDB管理**（`rice_guide`テーブル）。家族UIから登録・削除。未登録ならタブレットに非表示
24. **祖母「できた」ボタンはセンサー照合付き**。センサー確認済みのみスタンプ記録。未確認は`_unverified`で記録（confidence=0.0）
25. **`/api/tablet-record` は認証済み**（ローカル or タブレットトークン認証）
26. **炊飯量は単一設定**（food事ごとではなく「次に炊く量」1つ。クリアで非表示）
27. **薬スケジュールはDB管理**（`medicine_schedule`テーブル。家族UIから朝/昼/夜の時刻を設定）
28. **家族→タブレット伝言機能あり**（`family_prompts`テーブル。60分で自動期限切れ）
29. **iot-web再起動時にLINE通知**（`ExecStartPost` で `notify_url.sh` 実行）
30. **活動時間の指定はしない**（祖母に「ご飯の時間」等を指示しない設計方針）

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
