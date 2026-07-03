# インシデント & 恒久対策タイムライン

> `iot-life-support` の実運用で発生した障害と、その根本対策のコミット履歴。
> 単なる「バグ修正」ではなく「本番運用で起きたことに対する恒久対応」を時系列で並べる。
> 最終発表の「運用継続性 / 災害復旧」の軸で使う想定。

## 全体像 (5/15 → 7/2)

```
5/15  モンスタークラスター根治 + 87GBディスク解放
5/22  「事象発生時刻」バッジ導入 (家族UI)
6/3   ★ 大規模インフラ改修 (H100動的検出 / TailscaleFunnel移行 / メモリプロファイラ)
6/13  トイレ長時間滞在誤検知抑制 + LINE outbox
6/14  H100 ハブ電源喪失 (物理電源断)
6/16  祖母宅 IPv4 WAN 完全停止 (BIGLOBE / IPv6のみ稼働)
6/25  Pi 完全ダウン (物理電源断疑い) — 6日間気付かず
7/1   復旧 + journald永続化 + 週次システムスナップショット導入
7/2   Tailscale Funnel パブリックDNS失効 → funnel reset で復活
```

## 各インシデントの詳細

### 5/15 — モンスタークラスター根治 (`204e7af`)

- **症状**: 食事セッション集約が暴走、1セッションに 2,000〜5,000 イベントを含む「化け物セッション」を大量生成。「夕食 18:50開始、ended_at 翌朝05:54、event_count 4,950件」等
- **原因**: 集約対象に高頻度ソース (`bathroom_meter` 10秒毎、`bath_motion`、`camera`) が含まれ、時間ギャップ判定が永遠に切れない
- **対策**: `_load_unassigned_events` で高頻度ソースを除外、既存モンスター 22件を `confirmed=-1` で無効化
- **結果**: event_count 上限 66件、LINE遅延 1-21分 (過去は11時間超)
- **副次対応 (同コミット)**: `data/captures` に検知フレーム 74,431枚 = 87GB 蓄積を発見、`save_detections=False` に変更してディスク解放

### 5/22 — 時刻バッジ表示 (`9f54ffd`)

- **家族フィードバック**: 「LINE 通知が来た時刻ではなく、実際にセンサー反応した時刻を知りたい」
- **対策**: 家族UI 未対応通知一覧に `📅 HH:MM` バッジを目立つ位置に配置、メッセージから正規表現で時刻抽出 (4パターン対応)

### 6/3 — インフラ大改修 (4コミット)

#### `a4c14f1` H100 ハブ動的IP検出

- **症状**: T110×4 + T100 が **13日間** 沈黙。Tapoアプリでは正常動作
- **原因**: ルーター DHCP リース更新で全 Tapo 機器の IP が変動、`.env` の静的 `HUB_IP=192.168.0.**` (旧値) が陳腐化
- **対策**: `src/sensors/hub_discovery.py` を新設。キャッシュ → `.env` → ブロードキャスト探索 の3段フォールバック
- **効果**: 以降 IP が変わっても自動回復

#### `1842a98` Cloudflare Quick Tunnel → Tailscale Funnel 移行

- **症状**: Quick Tunnel は再起動のたびに URL が変わる → 家族のブックマーク・LINE webhook・タブレットを毎回更新
- **原因**: Quick Tunnel は仕様上「使い捨て URL」で固定不可
- **対策**: Tailscale Funnel に移行、URL永久固定 `https://tara0.taile9fa63.ts.net`。LINE webhook 自動再登録機構の依存も消滅

#### `5f4d006` tracemalloc プロファイラ

- **症状**: iot-monitor RSS が 5日で 1.2GB に達する報告
- **原因の特定**: `.env MEMORY_PROFILE=1` で 10日連続計測 → Python heap は完全に安定 (1562.8MB で頭打ち)、増加分はすべて dlib の C 拡張バッファと確定診断
- **結論**: 線形リーク無し、dlib のピーク時バッファ拡張のみ、~1.5GB で頭打ち → 再起動不要

#### `3fa8a2f` `rotate_logs` cron 恒久化

- L004 (87GB 問題) の再発防止保険として `data/captures` TTL を 14日→1日に短縮

### 6/13 — トイレ長時間滞在アラート誤検知 (`f658013`)

- **症状**: 家族に「トイレ滞在 371分/609分」等のアラート大量送信 (異常値)
- **原因**: `on_contact_change()` で `last_open` から close まで時間を測るが、トイレのドアを開けっぱなしにして家族が後で閉めるとその全期間を「滞在時間」と誤判定
- **対策**: `TOILET_DOOR_ABANDON_SECONDS=30分` を導入。30分超は「ドア放置」として警告スキップ

**同コミット副産物**:
- LINE 通信失敗時の outbox キューイング (`data/line_outbox.jsonl` + cron 毎分再送)
- DB 剪定スクリプト `prune_old_events.py` (将来の運用継続性のため)

### 6/14 15:39 — H100 物理ダウン

- 全 T110/T100 が同秒で同時停止 → H100 の物理電源断が最有力
- 動的検出 (6/3導入) が探索を継続するがブロードキャストでも発見不能

### 6/16 — 祖母宅 IPv4 WAN 完全停止 (BIGLOBE 契約要因)

- **症状**: LINE 通知エラー 1日 4,583件、IPv4 で外に一切出れず、IPv6 のみ健全
- **切り分け結果**:
  - Pi → 家庭内ルーター (LAN 内 GW) は正常
  - ルーター (IODATA WN-DEAX1800GR) → PR-500KI (NTT HGW) 経由の IPv4 WAN が完全死亡
  - IPv6 IPoE (BIGLOBE) は健全稼働
- **深掘り**: WN-DEAX1800GR の SOAP API で WAN 設定を取得、PR-500KI に「機器設定用パスワード初期設定画面」を発見 → HGW が初期化状態になっていた
- **一時対策**: `retry_line_outbox.py` cron でキュー再送

### 6/25 — Pi 完全ダウン (6日気付かず)

- Tailscale 経由 SSH が 6/25 17:30 から不通
- `EXT4-fs orphan cleanup on readonly fs` の痕跡 → 正常 shutdown を経ていない = 物理電源断が最有力
- ただし DB データを見ると `bathroom_meter` は 6/30 23:59 まで、`camera` は 6/28 03:20 まで受信 → **Tailscale 切断と Pi 完全停止は別タイミング**
- 家族から連絡なしで6日間気付けなかった = 最大の反省点

### 7/1 — 有線LAN + 電源入れ直しで復旧、恒久対策実装 (`08016b1`)

- **再発防止 A**: `journald` 永続化 (`Storage=persistent` + 500M/1month) — 次回クラッシュの直前ログを残す
- **再発防止 B**: 週次システムスナップショット (`scripts/system_snapshot.sh`, cron `0 4 * * 0`) — DB + 顔認識 + `.env` + systemd unit を tar.gz で 4週保持、SD破損時の復旧時間短縮

### 7/2 — Tailscale Funnel パブリック DNS 失効

- **症状**: LINE メニューコマンドが応答しない、webhook test で 400
- **原因**: 復旧後 Tailscale Funnel の DNS が `NXDOMAIN` (パブリックDNSから消失)
- **対策**: `sudo tailscale funnel reset && sudo tailscale funnel --bg 8000` で再登録 → A レコード `103.84.155.153/217` (Tailscale DERP relay) がパブリックDNSに復帰
- **確認**: LINE Platform (147.92.150.193) からの webhook POST が 200 OK で受信されるように

## 教訓 (発表で使える主張)

1. **本番運用は動かしてから初めて見える** — モンスタークラスター、DHCP変動、電源断、ISP契約変更…設計時に想定不可な事象が積み重なる
2. **「気付ける仕組み」が本体と同等に重要** — 6日気付かなかった反省から journald 永続化 + 週次スナップショットを追加
3. **恒久対策のコミットには根本原因 (Problem/Cause/Fix) を必ず記述** — 半年後の自分・後任のため
4. **外部依存 (ISP/Cloudflare/Tailscale) は必ず変わる** — Cloudflare Quick Tunnel → Tailscale Funnel の移行判断
5. **家族への影響を測定可能に** — 通知応答率 97% を保つには誤検知抑制が最優先課題
