# 引き継ぎメモ（最終更新: 2026-07-10）

> 認知症の祖母をIoTで支援するプロジェクトの開発引き継ぎドキュメント。
> 次回 Claude セッションで「**HANDOFF.md と PROGRESS.md を読んで続きを進めて**」と伝えればコンテキスト復元できる。

---

## 📌 ひとことサマリー（5/22 時点）

- **ステータス**: 完全に遠隔運用フェーズへ移行。モンスタークラスター・時間ズレ通知の根本治癒完了。家族UIの未対応通知一覧で対応する方式が確立
- **ブランチ**: `dev` 8コミット先行（5/15-5/22 セッションで実装）、main から計 **55+ コミット先行**
- **ネットワーク**: WiFi（IODATA-2G、wlan0=192.168.0.31）+ Tailscale (100.123.131.127)、**Tailscale Funnel で固定URL公開**（6/3〜、cloudflared は廃止済）
- **顔認識**: 祖母・はるか登録、受動収集 60px 閾値に調整済（5/14 までは80px でカット）
- **LINE通知**: マスタースイッチ ON、再通知（30分おき）は廃止 → 家族UI でスタック対応方式

### 🆕 5/4-5/5 で実装した主な変更（コミット c41a371 / e26f486 で保存済）

1. **全センサーのIP更新**（HUB_IP=192.168.0.30、RICE_COOKER_IP=192.168.0.24、CAMERA_IP=192.168.0.29）
2. **T100モーションセンサ対応**（contact_sensor.py に MotionSensor モジュール処理追加）
3. **SwitchBot 防水温湿度計（W3400010）の Manufacturer Data パーサー実装**
4. **タイムゾーンバグ修正**（recheck_pending.py で再通知5分連発バグの根本治癒）
5. **タブレット2段確認モーダル**（押し間違い防止 — 「いいえ、まだです」追加）
6. **家族UI 行動サマリ**（生センサーデータ→「何が起こったか」予測表示、event_summarizer.py）
7. **ライブカメラ映像**（家族UIから stream2 でMJPEG表示、camera_stream.py）
8. **食事写真をタブレット側に表示**（祖母が自分の食事を見て思い出すため）
9. **外食/朝食/昼食/夕食/間食ボタン**（家族UIから手動記録、broadcast対応）
10. **タブレット自動更新**（family-prompt 送信時に WebSocket 経由で即時反映+音声）
11. **未確定セッション → LINE確認フロー**（confirmed カラム追加、家族選択後に確定）
12. **BathDetector**（湿度+ドア+モーションで入浴候補検知、LINEで「誰がお風呂？」質問）
13. **ロック確認の事前化**（実行前に「ロックしますか？」確認、確認後ロック）
14. **通知の選択肢拡張**（「了解/対応不要(誤検知)」2択、炊飯器は5択）
15. **全家族アクション通知の broadcast**（誰が何を確認/解除/記録したか全員に共有）
16. **家族向け文言の自然化**（"event #1234"→"炊飯器の動作を「保温」として記録"）
17. **7日間アクティビティ ヒートマップのタブ式化**（食事/お風呂/トイレ/冷蔵庫/脱衣所別）
18. **ソース名の日本語化**（toilet_door→トイレのドア など）
19. **誤検知抑制の強化**: bath_motion / bathroom_meter / toilet_door を食事判定材料から除外、冷蔵庫単独食事判定停止、深夜は蓋開必須化
20. **夜間通知抑制**（1〜5時、緊急系のみ通す）
21. **トイレ長時間滞在アラート**（5分以上で CRITICAL 通知）
22. **学習状況可視化UI**（家族UIに「📚 システムの学習状況」セクション）

### 🆕 5/8 セッション追加機能（コミット 8f5c0ee / a4d5d48 / 047ffe7）

23. **受動的顔学習システム**:
    - 未識別の顔を自動で `data/faces/candidates/` に保存（連続抑制 60秒、1日上限 200件）
    - 家族UI `/family/face-learning` で遠隔ラベル付け
    - 祖母宅にいなくても自宅から学習データを蓄積できる
24. **LINEメニュー機能**:
    - 「メニュー」コマンドで主要機能へのボタン（家族UI/ガイド/顔学習/食事写真/意見・質問/困った時）
    - URI アクション + postback アクション混在対応
25. **意見ボックス機能**:
    - 「意見/質問/要望/バグ + 内容」コマンド
    - feedback テーブル保存 + admin に LINE 即時転送
26. **センサ可視化拡張**:
    - センサー反応回数（24h/7d）
    - 通知応答の詳細内訳（誤検知/不明/他家族/未対応）
    - 候補顔キューの深さ表示
27. **判定根拠の通知メッセージ含有**（session_confirm に「判定根拠: 炊飯器の蓋×2、冷蔵庫×3」）
28. **人物推定の時間重み付け**（直近2分以内のカメラ識別を高信頼度として優先）
29. **LINE操作ガイド作成** (`docs/guide/line-operation.md`):
    - 友達追加〜通知対応まで完全ガイド、`/guide/line-operation` で公開
30. **ガイドルートの .md 自動除去**（README.md の md 拡張子付きリンク対応）
31. **炊飯器保温パルス通知の根本治癒（再修正）**:
    - SQLite datetime('now') (UTC) と events.started_at (JST) のタイムゾーン不一致を Python 側 cutoff で修正
    - 蓋を開けていない時は LINE 問い合わせが一切発火しなくなる

### ⚠️ 既知の問題・運用観察（5/8 現在）

- 主要な誤検知問題は対処済 → 数日運用観察フェーズ
- 祖父さんは顔未登録（受動収集システムで自動収集中、後で遠隔ラベル付け予定）
- **dev → main マージ未実施**（48コミット先行、運用安定後にマージ予定）

### 🆕 5/11-5/22 セッション追加機能 (コミット 7コミット)

32. **トンネル systemd 化** (commit a4d5d48 以降):
    - cloudflared 自動更新で死んでも auto-restart で復活
    - start_tunnel.sh を foreground 化（wait $TUNNEL_PID）して systemd Type=simple 互換
    - update_webhook_url に最大5回×6秒のリトライ機構追加
33. **再通知廃止・家族UI スタック化** (commit 204e7af):
    - recheck_pending.py: LINE 再通知を廃止、24時間タイムアウトのみ
    - /api/pending-notifications で未対応一覧を返す
    - family.html に「📨 未対応の確認」セクション（30秒おき自動更新）
    - LINE Quick Reply と同等のボタンで UI からインライン対応可能
34. **モンスタークラスター根本治癒** (commit 204e7af):
    - sessions._load_unassigned_events で bathroom_meter / bath_motion / camera を
      クラスタリングから除外
    - 結果: event_count 上限 66件（過去5000件超）、LINE遅延 1-21分（過去11時間超）
    - 過去のモンスター22件を confirmed=-1 で無効化
35. **古いセッションのLINE通知ガード** (commit 204e7af):
    - 2時間以上前のセッションは LINE 通知しない（家族UIには表示継続）
    - 万一モンスター化しても深夜通知を防ぐ二重保険
36. **ディスク 87GB 解放** (commit 204e7af):
    - data/captures に検知フレームが7万枚以上累積
    - camera.py の save_detections デフォルトを False に変更
    - 受動的顔学習は別系統 data/faces/candidates/ で稼働継続
37. **顔学習閾値の柔軟化** (commit 4349e24, 204e7af):
    - CANDIDATE_MIN_FACE_SIZE: 80px → 60px、.env で動的調整可能
    - 候補顔のサイズフィルタ + 重複除外
    - clean_face_candidates.py 追加
38. **時刻バッジ表示** (commit 9f54ffd):
    - 家族UI 未対応通知に「📅 HH:MM」バッジを目立つ位置に表示
    - bath_classification / lock_confirm に時刻を含めるよう改良
    - メッセージから正規表現で時刻抽出（4パターン対応）

### ✅ 運用観察結果（5/19 時点で確認済）

- モンスタークラスター: **完全消滅** （直近3日で最大66件、過去5000件超）
- LINE 通知遅延: **1〜21分** （過去は11時間超の深夜通知あり）
- サービス無停止稼働: iot-monitor / iot-matter は 4〜8日連続稼働、再起動0回

### 🆕 5/23-6/3 セッション追加（インフラ恒久化）

| # | 変更 | コミット |
|---|---|---|
| 39 | **H100ハブIPの動的検出** (`src/sensors/hub_discovery.py`)。DHCPリース更新で HUB_IP=192.168.0.30 が陳腐化し T110×4/T100 が13日サイレントになった事故への恒久対策。キャッシュ→.env→ブロードキャスト探索の3段フォールバック | `a4c14f1` |
| 40 | **data/captures TTL を 14日→1日** へ短縮 + `rotate_logs.sh` を `cron 0 4 * * *` で恒久化。L004 (87GB問題) の再発防止保険 | `3fa8a2f` |
| 41 | **メモリリーク調査用 tracemalloc プロファイラ** (`src/memory_profiler.py`)。`.env MEMORY_PROFILE=1` で有効化、5分後ベースライン+30分毎スナップショット、結果は `logs/memory_profile.log`。Python heap は完全に安定、増加分はすべて dlib の C 拡張バッファと判明 | `5f4d006` |
| 42 | **Cloudflare Quick Tunnel → Tailscale Funnel 移行**。URL が永久固定 `https://tara0.taile9fa63.ts.net` に。`scripts/switch_to_tailscale_funnel.sh` で切替を一発化、LINE webhook 自動再登録機構の依存も消滅 | (本コミットで同梱) |

### 🆕 6/4-7/10 セッション追加（インフラ+安全性強化）

| # | 変更 | コミット |
|---|---|---|
| 43 | **トイレ長時間滞在アラート誤検知抑制**。open→close 30分以上は「ドア放置」と判定してアラートスキップ (609/371/287分 の異常アラート根治)。LINE outbox キューイング (`data/line_outbox.jsonl` + cron 毎分 retry) 併せて実装 | `f658013` (6/13) |
| 44 | **週次システムスナップショット** (`scripts/system_snapshot.sh`, cron `0 4 * * 0`)。DB + 顔認識 encodings + .env + systemd unit を tar.gz で 4週保持。SD破損時の復旧時間短縮 | `08016b1` (7/1) |
| 45 | **`journald` 永続化** (`/etc/systemd/journald.conf.d/40-rpi-volatile-storage.conf` に `Storage=persistent` + 500M/1month で上書き)。次回クラッシュの直前ログを残す | (手動設定、7/1) |
| 46 | **家族UI ログインにレート制限** (`slowapi`, `/family/login` に `5/15minute`)。4桁PIN + Tailscale Funnel 公開の総当たり対策 | (7/10) |
| 47 | **`requirements.txt` 導入** (`pip freeze` 105 パッケージ pinning)。SD破損時の再構築で完全再現可能に | (7/10) |
| 48 | **`dev` → `main` merge** (63コミット分)。本番運用ブランチのズレを解消 | (7/10) |

### ⚠️ 既知の問題・運用観察（7/10 現在）

- **メモリ使用**: iot-monitor の RSS は起動から階段状に増えて約 1.5GB で頭打ち（dlib のピーク時バッファ）。10日連続稼働で完全にフラット (`memory_profile.log` で確認済)、`MEMORY_PROFILE=0` に戻して常用OK
- **6/25-7/1 Pi 完全ダウン (6日間気付かず)**: 原因は物理電源断が最有力 (`EXT4-fs orphan cleanup on readonly fs` の痕跡)。復旧後 journald 永続化と週次スナップショットを追加、次回クラッシュ史を残せるように。**根本の再発防止 = 外部死活監視 (UptimeRobot 等) はまだ未実装**
- **7/2 Tailscale Funnel パブリック DNS 失効**: 復旧後の Pi 起動で Funnel の DNS が NXDOMAIN 化 → `sudo tailscale funnel reset && sudo tailscale funnel --bg 8000` で復活。次回同じ症状が出たら同じコマンドで即復活
- **6/13〜 IPv4 WAN 不安定 → 6/16 完全停止**: 祖母宅の PR-500KI (NTT HGW) が初期化状態、WN-DEAX1800GR (IODATA) はただの DHCP クライアント。BIGLOBE PPPoE 認証情報を PR-500KI に再入力する必要あり、詳細は下記 `## 🌐 祖母宅ネットワーク構成` 参照
- 顔登録: 祖母・はるか のみ。祖父・ゆきこ・みきこ・まきこ・なお は person 登録あるが encoding 未紐付（必要なら帰省時に登録）
- **pending_notifications 502件中 413件が 30日以上前** = 掃除されず累積。実害は小さいが今後 archive_pending_notifications 移動 cron を検討
- **未対応 TODO**: 外部死活監視 / 電源物理接続の固定 (物理作業) / sshd root ログイン可否見直し

### 🌐 祖母宅ネットワーク構成 (6/19 判明)

```
フレッツ光 → NTT GE-PON ONU (TA06005-B706)
          → NTT PR-500KI (HGW, 192.168.1.1) ← ★IPv4 PPPoE 担当だがリセット状態
          → IODATA WN-DEAX1800GR (WiFiルーター, 192.168.0.1, MAP-E対応) ← DHCP クライアント
          → 子機 (Pi/Tapo/カメラ/タブレット)
```

PR-500KI 管理画面: `http://192.168.1.1/ntt/` (Pi から SSH LocalForward 8081 で家からアクセス可)
Basic 認証: **過去に設定済でパスワード不明**、BIGLOBE PPPoE 認証情報も要再確認。復旧は NTT 116 電話 or 物理リセットボタン長押し + 再セットアップ。

---

## 🚨 外部死活監視 (healthchecks.io) — セットアップ手順

Pi が完全ダウンしても気付ける仕組み。6/25-7/1 の 6日間ダウンを気付けなかった反省から追加。

### セットアップ (1回だけ、5分)

1. https://healthchecks.io/ で無料アカウント作成 (email 認証のみ、クレカ不要)
2. **New Check** → Name: `iot-life-support`、Schedule: **Simple** / Period: **10 min** / Grace: **5 min**
3. できた **Ping URL** (例: `https://hc-ping.com/abc12345-...`) をコピー
4. Pi 側 `.env` に貼付:
   ```bash
   nano .env
   # HEARTBEAT_URL=https://hc-ping.com/<UUID>  ← ここを埋める
   ```
5. `bash scripts/heartbeat.sh` を手動実行して healthchecks.io ダッシュボードで "up" になるか確認
6. **Integrations** → Slack/Email/Webhook 好きな通知先を追加
   - 例: **Email** に自分のメール登録 → Pi が15分無音でメール届く
   - 例: **Webhook** に LINE Notify or 自作エンドポイント URL を登録

### 挙動

- Pi 側 cron `*/5 * * * *` が `heartbeat.sh` を実行、`HEARTBEAT_URL` に GET
- サービス全部 active なら `HEARTBEAT_URL` (成功)、どれか停止なら `HEARTBEAT_URL/fail` (失敗)
- healthchecks.io が **15分無音で "down" 判定** → 登録済 Integration 経由で即通知

### 一時無効化

`.env` の `HEARTBEAT_URL=` を空にすれば heartbeat.sh は何もしない。

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

## 📡 センサー設置状況（5/5 時点）

### ✅ 設置・稼働中
- **ラズパイ5**: WiFi (IODATA-2G, wlan0=192.168.0.31) / Tailscale active
- **Tapo P110M**（炊飯器コンセント）: 192.168.0.24, Matter経由 node_id=1
- **Tapo H100 ハブ**: 192.168.0.30
- **Tapo T110 × 4**: 冷蔵庫 / 風呂ドア / トイレドア / 炊飯器の蓋
- **Tapo T100 × 1**: 脱衣所モーション
- **Tapo C220 カメラ**: 192.168.0.29、stream1 (2K) で顔認識動作中
- **SwitchBot 防水温湿度計**: BLE 直接読取り、MAC=DE:64:44:06:49:2A
- **公開URL（6/3〜 永久固定）**: `https://tara0.taile9fa63.ts.net` — Tailscale Funnel 経由。cloudflared (Quick Tunnel) は廃止
- **LINE webhook**: `https://tara0.taile9fa63.ts.net/line/webhook`（URL固定なので動的更新は不要）

### ⏳ 未設置/今後
| 機材 | 設置予定場所 | 用途 |
|---|---|---|
| H100 ハブ ② | 脱衣所周辺 | 1個目で間に合う場合は不要 |
| T110 ⑤ | 歯ブラシスタンド | 歯磨き行動 |
| T110 ⑥ | シャンプーボトル | シャンプー使用 |
| T100 ② | 洗面所 | 洗面所滞在 |
| T100 ③ | （脱衣所追加） | 入浴中の動き精度UP |
| S200B ボタン ① | 玄関 | 訪問記録（コード未実装） |
| S200B ボタン ② | 祖母テーブル | 緊急/呼び出し（コード未実装） |
| P110M ②（追加運用） | ドライヤーコンセント | 髪洗い検知（HAIR_DRYER_NODE_ID 設定必要） |

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
| LINE「ヘルプ」に応答なし | webhook URL = `https://tara0.taile9fa63.ts.net/line/webhook` (固定) が登録済か確認 |
| 公開URL不明 | **固定**: `https://tara0.taile9fa63.ts.net` (もう変動しない、cloudflared 廃止済) |
| Tailscale Funnel が落ちた | `sudo tailscale funnel status` で確認、再有効化は `sudo tailscale funnel --bg 8000` |
| Tapo 機器が応答しない（DHCP IP変動） | `src/sensors/hub_discovery.py` が自動で broadcast 探索 → 再起動で自動回復。`data/discovered_hub_ip.txt` にキャッシュ |
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
