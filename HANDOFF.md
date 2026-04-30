# 引き継ぎメモ（最終更新: 2026-05-01）

> 認知症の祖母をIoTで支援するプロジェクトの開発引き継ぎドキュメント。
> 次回 Claude セッションで「**HANDOFF.md と PROGRESS.md を読んで続きを進めて**」と伝えればコンテキスト復元できる。

---

## 📌 ひとことサマリー

- **ステータス**: GW投入準備完了。**残り5日（5/1〜5/6）**で祖母宅設置。
- **ブランチ**: `main` ≒ `dev`（PR #1 でマージ済）／`future` は main 取り込み済 + 実験機能搭載
- **遠隔SSH**: ✅ Tailscale設定済（祖母宅移動後も `ssh taraberrypi` で接続可）
- **LINE通知**: 🔇 **マスタースイッチOFF中**（4/25〜開発期間中は静音）
- **物理機器テスト**: ⏳ 未実施（顔登録、H100/T110統合、実炊飯器電力しきい値確認）

---

## 🌐 リモート接続情報（Tailscale）

GW で祖母宅に Pi を移しても、自宅から SSH 可能。

### 接続コマンド
```bash
# PC側ターミナル（Tailscale起動済みで）
ssh taraberrypi   # PC側 .ssh/config に設定済（HostName tara0）
```

### Tailscale の状態
- **Pi のホスト名**: `tara0`（変えるなら `sudo tailscale set --hostname=taraberrypi`）
- **Pi の Tailscale IP**: `100.123.131.127`
- **PC（Windows）の Tailscale IP**: `100.80.182.87`
- **MagicDNS**: 有効（`tara0` だけでホスト名解決）
- **Tailscale SSH**: `--ssh` オプションで有効化済（鍵設定不要）

### PC側 SSH config（Windows: `C:\Users\tiita\.ssh\config`）
```
Host tara0.local        # ローカルWi-Fi (mDNS)
  HostName tara0.local
  User tara0

Host 192.168.11.11      # ローカルWi-Fi (直接IP)
  HostName 192.168.11.11
  User tara0

Host taraberrypi        # Tailscale 経由（外出先・祖母宅移動後）
    HostName tara0
    User tara0
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

VS Code Remote-SSH からも `taraberrypi` ホスト名で接続可能。

---

## 🌳 ブランチ状態（2026-05-01）

| ブランチ | 最新コミット | 内容 |
|---|---|---|
| **main** | `bb6c85e` | PR #1で dev からマージ済。本番デプロイ用 |
| **dev** | `2341dc7` | README にブランチ比較マトリクス追加 |
| **future** | `e80283c` | main マージ済 + 実験機能搭載 |

### `future` だけにある5機能
1. 📱 **タブレット用キオスクAPK** ([android/](android/))
2. 🔧 **GitHub Actions: APK自動ビルド** (`.github/workflows/build-apk.yml`)
3. 📊 **週間サマリー API** (`/api/weekly-summary`)
4. 📨 **複数LINE通知先基盤** (`notify_targets` テーブル)
5. 🗃️ **データアーカイブスクリプト** (`scripts/archive_old_data.py`)

### ブランチ保護（GitHub設定推奨）
- main: **force push 禁止 / 削除禁止** を設定推奨（GitHub の警告対応）
- 設定場所: https://github.com/tara0kun/iot-life-support/settings/branches

---

## 🔇 LINE通知マスタースイッチ（重要）

開発期間中の通知ノイズを抑えるため **マスタースイッチOFF** にしてある。

### 現状確認
```bash
cd ~/IoT && source venv/bin/activate
python scripts/toggle_notifications.py status
# notify_master_enabled が 0 なら OFF
```

### 開発再開時に必ず ON に戻す
```bash
python scripts/toggle_notifications.py on
```

### 影響範囲（OFF中も継続するもの）
- ✅ LINEからのコマンド返信（リンク・状況・タスク等）
- ✅ DBへのイベント記録、家族管理画面、タブレット表示
- ❌ 食事検知・ロック・お薬・お風呂・まとめ・週次・異常検知・ヘルスチェックの自動Push通知

詳細は [src/notifier.py](src/notifier.py) の `send_line_message()` 冒頭参照。

---

## 📅 直近の作業履歴（4/22以降）

### 2026-04-22
- LINE webhook双方向化（リンクで最新URL自動返信、トンネル起動時にwebhook URL自動登録）
- LINEコマンド体系（状況・最後の食事・タスク・済・ロック解除・ヘルプ）
- ロック解除PIN認証（4桁・5分有効・3回失敗で15分ロックアウト）
- 機器管理セクション常駐化（家族UI）
- 「状況」情報拡充、家族タスク役割分担、週次レポート
- タブレット音声読み上げ（Web Speech API）
- 異常検知（深夜炊飯器・無反応・冷蔵庫）

### 2026-04-24
- GW_SETUP.md大幅拡充
- ヘルスチェック強化（コンポーネント別状態判定）
- 家族デモ用モックデータ scenario 5
- タブレットUI調整（高齢者向けフォント・タッチ領域）
- 動的設定機能（家族UIで通知ON/OFF・しきい値編集）
- 7日ヒートマップ、週次レポートPDF対応
- ログローテーション、DB復元スクリプト
- Service Workerオフライン対応
- 家族向け説明書＋操作マニュアル `/family/manual`

### 2026-04-25
- LINE通知マスタースイッチ追加（`scripts/toggle_notifications.py`）
- マスタースイッチOFF（開発期間中）

### 2026-04-27
- 祖母用タブレット キオスクAPK 実装（future）
- GitHub Actions APK自動ビルド（future）
- ブランチ整理（future = main + 実験機能）
- README にブランチ比較マトリクス追加
- family_manual.html に「今後の予定」セクション追加

### 2026-05-01
- **Tailscale導入** — リモートSSH 設定完了
- VS Code Remote-SSH 経由で Tailscale 接続成功

---

## 🚧 GW投入までの残タスク

### 🔴 高優先度（GW前必須）
- [ ] **顔登録テスト** — `python scripts/register_face.py --person-id 1 --name 祖母`
- [ ] **H100電源投入 → T110統合テスト**
- [ ] **実際の炊飯器での電力しきい値確認**
- [ ] **全センサー統合テスト** — monitor.py通しで DB → UI → LINE通知
- [ ] **cron登録**（[GW_SETUP.md](GW_SETUP.md) Step 10 参照）
- [ ] **LINE通知マスタースイッチを ON に戻す**（投入直前）

### 🟡 中優先度（GW中に対応可）
- [ ] P110M 2台目セットアップ
- [ ] T110残り2台 + T100 のH100ペアリング
- [ ] 祖母用タブレット端末の調達
- [ ] 祖母宅Wi-Fi接続テスト

### 🟢 検討中（要判断）
- [ ] Named Tunnel化（Cloudflareアカウント+独自ドメイン年1,500円）
- [ ] APKビルド＆タブレット導入（future ブランチ、要 GitHub Actions or ローカルビルド）
- [ ] 訪問販売対応（要 Tapo D230S1 ドアベル、Phase 2）
- [ ] 服薬自動化（要 T110＋薬箱、Phase 2）

---

## 🔧 起動・操作方法

### 開発サーバ
```bash
cd ~/IoT
source venv/bin/activate
uvicorn src.web.app:app --host 0.0.0.0 --port 8000 --reload
```

### 本番（systemd）
```bash
sudo systemctl status iot-matter iot-web iot-monitor
sudo systemctl restart iot-web    # コード変更後
```

### モックデータ投入
```bash
python scripts/seed_mock_data.py --clear-all --days 5
python scripts/seed_mock_data.py --clear --scenario 5  # 家族デモ用
```

### LINE通知 ON/OFF
```bash
python scripts/toggle_notifications.py on     # ON
python scripts/toggle_notifications.py off    # OFF
python scripts/toggle_notifications.py status # 状態確認
```

### バックアップ・復元
```bash
bash scripts/backup_db.sh                # 安全バックアップ
python scripts/restore_db.py             # 一覧
python scripts/restore_db.py --latest    # 最新から復元
```

### 異常検知・ヘルスチェックの個別実行
```bash
python scripts/anomaly_check.py
python scripts/health_check.py
python scripts/log_summary.py --hours 6
```

---

## 📚 主要ドキュメント

| ファイル | 内容 |
|---|---|
| [PROGRESS.md](PROGRESS.md) | 進捗記録（冒頭に「現状ダイジェスト」） |
| [README.md](README.md) | プロジェクト全体概要、ブランチ運用、機能比較 |
| [GW_SETUP.md](GW_SETUP.md) | GW現地設置の全手順（出発前〜24時間監視〜ロールバック） |
| [FAMILY_GUIDE.md](FAMILY_GUIDE.md) | 家族向けガイド（旧版、現在は `/family/manual` で参照） |
| [src/web/templates/family_manual.html](src/web/templates/family_manual.html) | 家族向け説明書＋操作マニュアル（HTML、印刷でPDF化可） |
| [android/README.md](android/README.md) | キオスクAPKのビルド・インストール手順（future ブランチ） |

---

## 🔑 機密情報の保管場所

`.env` ファイル（git管理外）に以下を保存：
- `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET` / `LINE_USER_ID`
- `TAPO_USERNAME` / `TAPO_PASSWORD`
- `CAMERA_USERNAME` / `CAMERA_PASSWORD`
- `FAMILY_PASSWORD` / `TABLET_TOKEN`
- `GRANDMA_WIFI_SSID` / `GRANDMA_WIFI_PASS`

LINE公式アカウント: `@428cbmzr`
LINE Developers コンソール: https://developers.line.biz/console/

---

## 🆘 トラブル時の参照

| 状況 | 参照 |
|---|---|
| ラズパイに繋がらない | [GW_SETUP.md トラブルシューティング](GW_SETUP.md) |
| LINE通知が止まらない | `python scripts/toggle_notifications.py off` |
| LINE webhook が応答しない | LINE Developers でWebhook URL確認、`bash scripts/start_tunnel.sh` |
| DB が壊れた | `python scripts/restore_db.py --latest --force` |
| センサーが動かない | `sudo journalctl -u iot-monitor -f` |
| Cloudflare Tunnel URL が変わった | LINE に「リンク」と送ると最新URL返信 |

---

## 📞 次回 Claude セッションの開始フレーズ

```
HANDOFF.md と PROGRESS.md を読んで現状を把握してから、続きを進めてください。
```

または具体的なタスクがあるなら：

```
HANDOFF.md と PROGRESS.md を読んでから、[X] を進めてください。

例:
- GW前の最終チェックをしたい
- 顔登録テストを進めたい
- futureブランチのキオスクAPKをビルドしたい
- 全センサー統合テストをしたい
```
