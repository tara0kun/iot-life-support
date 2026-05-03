# 引き継ぎメモ（最終更新: 2026-05-03）

> 認知症の祖母をIoTで支援するプロジェクトの開発引き継ぎドキュメント。
> 次回 Claude セッションで「**HANDOFF.md と PROGRESS.md を読んで続きを進めて**」と伝えればコンテキスト復元できる。

---

## 📌 ひとことサマリー

- **ステータス**: 祖母宅でセットアップ作業中。**ラズパイ＋炊飯器 P110M は稼働済み**、他センサー（H100/T110/T100/カメラ/タブレット）は**未設置**
- **ブランチ**: `dev` で開発継続中（5/2朝〜5/3未明にかけて 24コミット追加）。`main` へのマージは GW投入完了後にまとめて実施予定
- **遠隔SSH**: ✅ Tailscale 経由で `ssh taraberrypi` 接続可（祖母宅でも自宅でも）
- **LINE通知**: ✅ マスタースイッチ ON、家族3名（ゆきこ・みきこ・まきこ）が LINE 自己登録済み、孫(LINE_USER_ID) と合わせて4人にブロードキャスト
- **炊飯器**: ✅ 蓋開保温パルスや過熱補正の誤検知問題を多層対策（蓋開抑制 + 曖昧電力家族問い合わせ + 学習による自動分類）

---

## 🌐 リモート接続情報（Tailscale）

GW で祖母宅に Pi を移しても、自宅から SSH 可能。

### 接続コマンド
```bash
ssh taraberrypi   # PC側 .ssh/config に設定済（HostName tara0）
```

### Tailscale の状態
- **Pi のホスト名**: `tara0`
- **Pi の Tailscale IP**: `100.123.131.127`
- **PC（Windows）の Tailscale IP**: `100.80.182.87`
- **MagicDNS**: 有効（`tara0` だけでホスト名解決）

### 祖母宅ネットワーク（5/2 設置時に判明）
- **接続方式**: 有線LAN（eth0）
- **Pi のローカルIP**: `192.168.0.23`
- **サブネット**: `192.168.0.0/24`
- ⚠️ 自宅（`192.168.11.x`）と異なるため、**Tapo機器のIPは全て新サブネット側に変更必須**

---

## 🌳 ブランチ状態（2026-05-03）

| ブランチ | 内容 |
|---|---|
| **main** | 5/2朝時点（dev/cc5e627 相当）。未マージ多数。GW投入後にmergeする想定 |
| **dev** | 開発継続中。**祖母宅Pi はこのブランチで稼働中**（HANDOFF.md からは仕様変更） |
| **future** | 実験機能（タブレットAPK、データアーカイブ等） |

> ⚠️ **HANDOFF.md 当初設計**では「祖母宅Pi=main」だったが、5/2-5/3 の高頻度修正により dev で運用中。GW投入完了後に dev → main の squash merge を行う方針。

### 5/2〜5/3 のコミット履歴（dev、新→古）

| 区分 | 主な変更 |
|---|---|
| **炊飯器ロジック** | idle_confirm 600秒、蓋開抑制、曖昧電力家族問い合わせ、学習による自動分類、蓋開必須フラグ |
| **人物割当** | 未確定セッション LINE Quick Reply、家族による割当、近接統合（自動60分・手動90分）、家族の自己LINE登録機構（自由名対応） |
| **通知改善** | 全通知を家族全員にブロードキャスト、actionable 化（✓確認したボタン）、完了通知＋再通知＋タイムアウト |
| **新機能準備** | ドライヤー P110M による髪洗い検知、SwitchBot 防水温湿度計（コード準備のみ） |
| **UI修正** | タブレット否定アラート非表示、タブレットスタンプ「髪洗った」追加、家族UI複数箇所のはみ出し修正、フィルタ折り返し |
| **削除** | 家族タスク（care_tasks）機能を全面削除 |
| **データ整理** | seed 「母」を「ゆきこ」に統合・削除、5/3朝の保温パルス誤検知データクリーンアップ |

---

## 🔇 LINE通知マスタースイッチ

✅ **ON**（5/2 14:31〜）

```bash
# 状態確認
python scripts/toggle_notifications.py status

# OFF にする（開発時のみ）
python scripts/toggle_notifications.py off
```

---

## 📡 センサー設置状況

### ✅ 設置済み・稼働中
- **ラズパイ5**: 有線LAN 192.168.0.23 / Tailscale active
- **Tapo P110M**（炊飯器コンセント）: Matter経由 node_id=1, threshold=100W, idle_confirm=600秒
- **iot-matter / iot-web / iot-monitor**: 全てsystemd active
- **Cloudflare Tunnel**: `https://database-difficulty-approx-natural.trycloudflare.com`（再起動時にURL変わる）
- **LINE webhook**: 上記URL登録済み

### ⏳ 未設置
| 機材 | 設置予定場所 | 用途 |
|---|---|---|
| H100 ハブ ① | キッチン | T110/T100の親機 |
| H100 ハブ ② | 脱衣所周辺 | 同上（広さ次第） |
| T110 ① | 炊飯器の蓋 | **蓋開検知（誤検知抑制の鍵）** |
| T110 ② | 冷蔵庫ドア | 食事行動補助 |
| T110 ③ | 浴室ドア | 入浴開始/終了 |
| T110 ④ | トイレドア | トイレ回数 |
| T110 ⑤ | 歯ブラシスタンド | 歯磨き行動 |
| T110 ⑥ | シャンプーボトル | シャンプー使用 |
| T100 ① | IHコンロ前 | コンロ前滞在 |
| T100 ② | 洗面所 | 洗面所滞在 |
| T100 ③ | 脱衣所 | 入浴中の動き |
| C220 カメラ | リビング出入口 | 人物検知（顔認識は後回し） |
| S200B ボタン ① | 玄関 | 訪問記録（家族滞在トラッキング、コード未実装） |
| S200B ボタン ② | 祖母テーブル | 緊急/呼び出し（コード未実装） |
| P110M ②（追加運用） | ドライヤーコンセント | 髪洗い検知 |
| SwitchBot 防水温湿度計（**未購入**） | 浴室内 | シャワー使用検知（コードのみ準備済み） |
| Androidタブレット | 祖母テーブル | 祖母用UI |

### Tapoアプリでデバイスをリネームすればコード自動対応
T110/T100の場所別エイリアス（`src/monitor.py:_alias_to_source`）:
- 「炊飯器」or「炊飯器の蓋」 → `rice_cooker_lid`
- 「冷蔵庫」 → `fridge`
- 「浴室ドア」 → `bath_door`
- 「脱衣所」 → `bath_motion`
- 「トイレ」 → `toilet_door`
- 「歯ブラシ」or「歯ブラシスタンド」 → `toothbrush`
- 「シャンプー」or「シャンプーボトル」 → `shampoo_bottle`

---

## 👥 LINE 登録済み家族

| person_id | 名前 | 登録方法 |
|---|---|---|
| 0 | 未確定 | sentinel |
| 1 | 祖母 | seed |
| 3 | 祖父 | seed |
| 4 | ゆきこ | LINE自己登録（5/2） |
| 5 | みきこ | LINE自己登録（5/2） |
| 6 | まきこ | LINE自己登録（5/2） |
| - | 孫(LINE_USER_ID) | `.env` 経由（自己登録なし） |

> 💡 **孫の自己登録未実施**: LINEで「登録 孫」と送れば persons に追加される。送るかどうかはお好み。

---

## 🍚 炊飯器の検知ロジック（5/3時点）

### 多層フィルタの全体像
```
[電力 >= 700W]                  → 自動「炊飯」確定
[電力 100〜700W]
  ├ 蓋開30秒以内              → 抑制（イベント記録なし）
  ├ 学習データ類似 ≥3件・80%同意 → 自動分類
  └ それ以外                    → 家族にLINE Quick Reply 問い合わせ
                                  [炊飯][保温][蓋開のみ][不明]

[セッション集約]
  ├ 蓋センサー稼働中（過去24h以内に rice_cooker_lid イベントあり）
  │     → 炊飯器単独 power_on は食事と認めない
  │     → 蓋開と組合せて初めて食事認定
  └ 蓋センサー未設置/未稼働
        → 旧来通り炊飯器単独でも食事認定
```

### 学習機構
家族が「炊飯/保温/蓋開のみ/不明」を選択するたび `rice_classifications` テーブルに保存。
- 特徴量: `power_w`, `hour_of_day`, `lid_recently_opened`
- 類似ケース（power±50W, hour±2h, lid同じ）が3件以上集まり 80%以上同分類なら自動判定
- 自動判定したものは学習対象から除外（再帰偏り防止）

### 5/3朝の検証で判明したこと
- **保温中**でも 200〜400W のパルスが発生する（蓋開時の温度補正、内部加熱維持）
- これが100Wしきい値だと「power_on 誤検知」 → 誤った食事カウントになる
- **対策**: 蓋センサー設置 + 学習機構 + 厳格モード（蓋センサー稼働後は自動切替）

---

## 🚧 残タスク（優先度順）

### 🔴 高優先度（GW投入前必須）

- [ ] **物理セットアップ**（H100×2 + T110×6 + T100×3 + C220 + タブレット）
  - Tapoアプリで祖母宅Wi-Fiに切替＋エイリアス設定
  - `.env` の `HUB_IP` / `CAMERA_IP` を新サブネット（192.168.0.x）の値に更新
  - `sudo systemctl restart iot-monitor` で反映
- [ ] **顔登録テスト**（C220設置後、`python scripts/register_face.py --person-id 1 --name 祖母`）
- [ ] **全センサー統合テスト** — DB→UI→LINE通知の一連確認
- [ ] **cron登録** — DBバックアップ・ヘルスチェック・recheck_pending・各種通知

### 🟡 中優先度（GW中対応可）
- [ ] P110M ② をドライヤーに転用、`.env` の `HAIR_DRYER_NODE_ID` 設定
- [ ] SwitchBot 防水温湿度計 購入（〜3000円）→ `bleak` インストール → 浴室設置
- [ ] S200B ボタン × 2 のコード対応（玄関訪問記録 + 緊急呼出）
- [ ] 顔認識統合（C220 → face_id.identify → events に person_id 自動付与）

### 🟢 検討中（着手判断要）
- [ ] Named Tunnel化（URL固定、年1500円）
- [ ] futureブランチ機能のmainマージ判断
- [ ] dev → main 統合（GW投入完了後）

---

## 🛠 主要スクリプト＆設定

### 開発サーバ
```bash
cd ~/IoT && source venv/bin/activate
uvicorn src.web.app:app --host 0.0.0.0 --port 8000 --reload
```

### 本番（systemd）
```bash
sudo systemctl status iot-matter iot-web iot-monitor
sudo systemctl restart iot-web    # コード変更後
```

### LINE通知 ON/OFF
```bash
python scripts/toggle_notifications.py on
python scripts/toggle_notifications.py off
python scripts/toggle_notifications.py status
```

### 異常検知・ヘルスチェック手動実行
```bash
python scripts/anomaly_check.py
python scripts/health_check.py
python scripts/log_summary.py --hours 6
```

### バックアップ・復元
```bash
bash scripts/backup_db.sh
python scripts/restore_db.py             # 一覧
python scripts/restore_db.py --latest    # 最新から復元
```

### 未対応LINE通知の再通知（cron推奨: */5 * * * *）
```bash
python scripts/recheck_pending.py
```

---

## 📚 主要ドキュメント

| ファイル | 内容 |
|---|---|
| HANDOFF.md（本ファイル） | 引き継ぎメモ（最新状態） |
| [PROGRESS.md](PROGRESS.md) | 進捗記録 |
| [README.md](README.md) | プロジェクト全体概要、ブランチ運用 |
| [GW_SETUP.md](GW_SETUP.md) | GW現地設置の全手順（5/2刷新版） |
| [src/web/templates/family_manual.html](src/web/templates/family_manual.html) | 家族向け説明書（HTML） |

---

## 🔑 機密情報の保管場所

`.env` に以下:
- `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET` / `LINE_USER_ID` / `LINE_ALLOWED_SENDERS`
- `TAPO_USERNAME` / `TAPO_PASSWORD`
- `CAMERA_USERNAME` / `CAMERA_PASSWORD` / `CAMERA_IP`
- `HUB_IP`
- `FAMILY_PASSWORD`（5/2に **`1488`** に変更）/ `TABLET_TOKEN`
- `GRANDMA_WIFI_SSID` / `GRANDMA_WIFI_PASS`
- `RICE_COOKER_THRESHOLD_W=100` / `RICE_COOKER_IDLE_CONFIRM=600`
- `HAIR_DRYER_NODE_ID=0`（未設定 = ドライヤー監視オフ）
- `SWITCHBOT_METER_ENABLED=0`（未購入）

---

## 🆘 トラブル時の参照

| 状況 | 対処 |
|---|---|
| ラズパイに繋がらない | `ssh taraberrypi`（Tailscale）/ `ssh tara0@tara0.local`（mDNS） |
| LINE通知が止まらない | `python scripts/toggle_notifications.py off` |
| LINE「ヘルプ」に応答なし | webhook URL不整合の可能性。`bash scripts/notify_url.sh` で再登録 |
| 公開URL不明 | LINE に「リンク」と送る or `cat data/tunnel_url.txt` |
| DB が壊れた | `python scripts/restore_db.py --latest --force` |
| 炊飯器を開け閉めしただけで食事検知される | T110を蓋に貼って Tapoアプリで「炊飯器」とリネーム → 自動的に厳格モードに切替 |
| LINE通知が孫1人にしか届かない | `LINE_ALLOWED_SENDERS` に複数入れる、または家族に「登録 名前」を送ってもらう |

---

## 📞 次回 Claude セッションの開始フレーズ

```
HANDOFF.md と PROGRESS.md を読んで現状を把握してから、続きを進めてください。
```

または具体的タスクで:
```
HANDOFF.md を読んで、H100ハブの物理セットアップを進めたい
```
