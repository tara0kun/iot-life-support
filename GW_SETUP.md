# GW設置手順書（2026-04-29〜05-06）

祖母宅でのIoTシステム設置手順。上から順にやれば完了する。

> ⚠️ **設置前に**: このドキュメントを印刷するか、スマホで開けるようにしておく（ラズパイがネットに繋がる前は手元のメモが頼り）

---

## 📋 出発前チェックリスト（自宅で完了させる）

### 持ち物
- [ ] **ラズパイ5本体**＋電源アダプタ（5V/5A 推奨）
- [ ] **microSDカード** が刺さっていること
- [ ] **Tapo P110M ×2**（炊飯器/予備）
- [ ] **Tapo H100 ハブ**＋電源アダプタ
- [ ] **Tapo T110 ×3**（冷蔵庫/浴室ドア/予備）＋ボタン電池予備
- [ ] **Tapo T100 ×1**（脱衣所モーション）＋電池予備
- [ ] **Tapo C220 カメラ**＋電源アダプタ＋有線LANケーブル（任意）
- [ ] **Androidタブレット**（10インチ程度、充電器付き）
- [ ] **延長コード**（必要な箇所分）
- [ ] **両面テープ・マジックテープ**（センサー固定用）
- [ ] **カメラ固定具**（フック、スタンド、結束バンド等）
- [ ] **HDMIケーブル＋小型モニタ**（万一ラズパイがネット繋がらない時のため）
- [ ] **USBキーボード**（同上）

### 自宅で動作確認しておく
```bash
cd ~/IoT && source venv/bin/activate

# 1. systemd 全部 active か
sudo systemctl status iot-matter iot-web iot-monitor

# 2. LINE通知が届くか
python -c "from src.notifier import send_line_message; send_line_message('GW出発前テスト')"

# 3. Cloudflare Tunnel が自動起動するか
crontab -l | grep tunnel

# 4. webhook URL が登録されているか（LINE Developers コンソールで確認）
#    https://developers.line.biz/console/

# 5. .env の必須項目チェック
grep -E '^(LINE_CHANNEL_ACCESS_TOKEN|LINE_CHANNEL_SECRET|LINE_USER_ID|TABLET_TOKEN|FAMILY_PASSWORD|GRANDMA_WIFI_SSID|GRANDMA_WIFI_PASS)=' .env
```

### 自宅で設定しておくと現地で楽
- [ ] **family メンバーをDBに登録済み**（祖母/母/祖父/孫）
- [ ] **薬スケジュール**を `/family` で登録済み
- [ ] **家族タスク**を必要なら `/family` の家族タスクセクションで登録
- [ ] **DBバックアップ**を取得（`data/iot.db` をUSBにコピー）

### 出発前に最終 git pull
```bash
cd ~/IoT
git checkout main
git pull origin main  # devで開発したものをmainにマージ済みであること
```

---

## Step 1: Wi-Fi接続（5分）

```bash
# ラズパイを祖母宅のWi-Fiに接続
bash scripts/setup_grandma_wifi.sh

# 接続確認
ping -c 3 google.com

# IPアドレスを確認（メモしておく）
hostname -I
```

祖母宅Wi-Fi情報は `.env` の `GRANDMA_WIFI_SSID` / `GRANDMA_WIFI_PASS` に記載済み。

**繋がらないとき**:
- 2.4GHz帯か確認（5GHzだとTapoが繋がらない）
- パスワードに特殊文字があれば `\` でエスケープ
- ルーターのMACアドレスフィルタリング解除を依頼

---

## Step 2: ラズパイ設置（5分）

1. ルーター付近にラズパイを設置（電源確保＋Wi-Fi電波最強）
2. 電源投入
3. systemdサービスが自動起動するのを待つ（約1分）

```bash
# サービス確認（全部 active であること）
sudo systemctl status iot-matter iot-web iot-monitor

# active でないものがあれば
sudo systemctl restart <サービス名>
sudo journalctl -u <サービス名> -n 50
```

---

## Step 3: Cloudflare Tunnel 起動確認（5分）

ラズパイ起動から30秒後に自動起動するはず。

```bash
# プロセス確認
pgrep -fa cloudflared

# URLが発行されたか確認
cat data/tunnel_url.txt

# 手動で再起動が必要な場合
bash scripts/start_tunnel.sh
```

LINEに `🌐 IoTシステムの公開URLが更新されました` が届けば成功。
**「✅ webhook更新OK」** も含まれていることを確認（webhookも自動登録されている）。

---

## Step 4: P110M（炊飯器用スマートプラグ）設置（10分）

1. 炊飯器のプラグを抜く
2. P110Mをコンセントに差す
3. 炊飯器のプラグをP110Mに差す
4. **TapoアプリでMatter再ペアリング**（祖母宅Wi-Fiに繋がるよう）

```bash
# 電力読み取りテスト
source venv/bin/activate
python scripts/test_p110m.py
```

期待値（ZOJIRUSHI NW-VC10）:
- 待機中: 0〜5W
- **保温中: 約15W**
- **炊飯中: 約1100W**

`.env` の `RICE_COOKER_THRESHOLD_W=100` で「炊飯/保温中」を判定。

**もし炊飯器の値が想定と大きく違う場合:**
```bash
nano .env
# RICE_COOKER_THRESHOLD_W=100 を適切な値に変更
sudo systemctl restart iot-monitor
```

---

## Step 5: H100ハブ + T110/T100 ペアリング（20分）

1. **H100の電源を入れる**（ルーター付近がベスト）
2. 各T110/T100の絶縁フィルムを剥がして起動
3. **TapoアプリでH100経由でペアリング**
4. 各センサーに**エイリアス**を設定（重要）:
   - 冷蔵庫の T110 → エイリアス**「冷蔵庫」**
   - 浴室ドアの T110 → エイリアス**「浴室ドア」** ← `monitor.py` がこの名前で判別
   - 脱衣所の T100 → エイリアス**「脱衣所」**
5. 物理的に貼付：
   - 冷蔵庫: 観音開きの**よく使う側**ドアに貼付（本体: ドア側 / マグネット: 本体側）
   - 浴室ドア: ドアと枠の間
   - 脱衣所: 動きを検知しやすい棚や壁

```bash
# H100経由でセンサー認識確認
source venv/bin/activate
python -c "
from kasa import Discover
import asyncio, os
async def check():
    hub_ip = os.environ.get('HUB_IP') or input('HUB_IP: ')
    dev = await Discover.discover_single(hub_ip)
    await dev.update()
    for child in dev.children:
        print(f'{child.alias}: {child.device_id}')
asyncio.run(check())
"

# 開閉してログに記録されるか
sudo journalctl -u iot-monitor -f | grep -E 'open|close|motion'
```

**エイリアス変更後に必ず**: `sudo systemctl restart iot-monitor`

---

## Step 6: C220カメラ設置（15分）

1. **壁時計の横**に設置（キッチン全体を見渡せる高い位置）
2. 電源ケーブルを壁沿いに配線（祖母が引っかからないよう）
3. TapoアプリでWi-Fi再ペアリング
4. **カメラアカウント**（Tapoアカウントとは別）のID/PWを `.env` に記録：
   ```bash
   nano .env
   # CAMERA_USERNAME=...
   # CAMERA_PASSWORD=...
   ```
5. RTSP接続テスト：
   ```bash
   python scripts/test_camera.py
   ```

設置候補: 壁時計の横（キッチン全体を見渡せる高い位置）

---

## Step 7: 顔登録（10分）

祖母がキッチンにいるときに実行：
```bash
source venv/bin/activate
python scripts/register_face.py --person-id 1 --name 祖母
```

カメラの前に立ってもらい、数秒待つ。
「顔を検出しました」と出れば成功。複数回登録すると精度向上。

母・祖父も登録：
```bash
python scripts/register_face.py --person-id 2 --name 母
python scripts/register_face.py --person-id 3 --name 祖父
```

---

## Step 8: タブレット設置（15分）

1. タブレットを祖母宅Wi-Fiに接続
2. ブラウザで `http://[hostname].local:8000/tablet` を開く
   - mDNSが効かない場合は `http://<ラズパイIP>:8000/tablet`
3. **「ホーム画面に追加」** で PWA としてインストール
4. **設定で画面ロック時間を「無し」または30分以上**に変更
5. **音声読み上げトグル** 🔊 を確認（ヘッダー右）
6. **充電ケーブル接続したまま**テーブルに立てかける

### 祖母への説明（重要）
> 「これはおばあちゃんの記録帳だよ。今日なにをしたか勝手に記録されるの。お花も育つよ。」

> 「困ったら『できた』ボタンを押してね。」

**説明しないこと**: 監視カメラのこと、家族編集機能のこと、ロック機能のこと

---

## Step 9: 動作確認テスト（30分）

### 9-1: 各センサー個別確認
| センサー | テスト | 期待 |
|---|---|---|
| P110M | 炊飯器の電源を入れる | DB に `power_on` イベント |
| T110 冷蔵庫 | ドア開閉 | DB に `open` `close` |
| T110 浴室 | 浴室ドア開閉 | DB に `open` `close` |
| T100 脱衣所 | 脱衣所で動く | DB に `motion` |
| C220 | キッチンに人が立つ | `camera` `person_detected` |

```bash
# リアルタイム確認
sudo journalctl -u iot-monitor -f
```

### 9-2: タブレットUI確認
- [ ] 時計が動いている
- [ ] スタンプカードが正しく表示
- [ ] お花が咲いている / 育っている
- [ ] 「最後に食べたのは」が正しい
- [ ] アラートが適切に出る/出ない
- [ ] 「できた」ボタンが押せる
- [ ] 音声読み上げが動作（家族からメッセージ送って確認）

### 9-3: 家族UI確認
- [ ] `/family` にログインできる
- [ ] 機器管理セクションでロック/解除ボタンが見える
- [ ] かんたん記録ボタンが動く
- [ ] イベント一覧に当日のイベントが出る
- [ ] 家族タスクの追加・削除ができる
- [ ] 薬スケジュールの設定ができる
- [ ] 「タブレットに伝える」でメッセージ送信ができる

### 9-4: LINE双方向確認
LINEで以下を順に送る：
- [ ] 「ヘルプ」 → コマンド一覧
- [ ] 「状況」 → 今日の状況サマリー
- [ ] 「タスク」 → 家族タスク一覧
- [ ] 「リンク」 → 最新URL

### 9-5: 統合テスト（食事検知 → ロック → 通知）
1. 炊飯器を稼働させる
2. しばらく待ってカメラに祖母（または代理）が映る
3. **monitor.py が食事を検知** → `meal_sessions` に記録
4. **2回目検知 → 自動ロック発動** → LINE通知
5. **「ロック解除」** とLINEに送る → 確認コード受信
6. 確認コード返信 → 解除完了通知

---

## Step 10: cron登録（5分）

GW現地で必ず登録（自宅環境のcronは持ち越されないので）：

```bash
crontab -e
```

以下を追加：
```cron
# Cloudflare Tunnel 起動30秒後に自動起動
@reboot sleep 30 && cd /home/tara0/IoT && bash scripts/start_tunnel.sh

# 毎日3時 DBバックアップ
0 3 * * * cd /home/tara0/IoT && bash scripts/backup_db.sh

# 5分おき ヘルスチェック
*/5 * * * * cd /home/tara0/IoT && bash scripts/health_check.sh

# お薬チェック
0 9,12 * * * cd /home/tara0/IoT && venv/bin/python scripts/scheduled_notify.py medicine

# お風呂チェック
0 18 * * * cd /home/tara0/IoT && venv/bin/python scripts/scheduled_notify.py bath

# 1日のまとめ
0 22 * * * cd /home/tara0/IoT && venv/bin/python scripts/scheduled_notify.py summary

# 家族タスクリマインダー（時刻に担当者へ）
0 7-22 * * * cd /home/tara0/IoT && venv/bin/python scripts/scheduled_notify.py care_tasks

# 週次レポート（毎週日曜22時）
0 22 * * 0 cd /home/tara0/IoT && venv/bin/python scripts/weekly_report.py

# 異常検知（10分おき）
*/10 * * * * cd /home/tara0/IoT && venv/bin/python scripts/anomaly_check.py
```

確認：
```bash
crontab -l
```

---

## Step 11: 家族のスマホ設定（10分）

1. 母のスマホで `/family` にアクセス（LINEに届いているURL）
2. パスワード `.env` の `FAMILY_PASSWORD` でログイン
3. ブックマーク追加・ホーム画面に追加
4. 使い方を説明：
   - 「かんたん記録」: おばあちゃんから報告を受けたら押す
   - 「家族タスク」: 担当を決めて時刻設定するとリマインドが来る
   - 「タブレットに伝える」: おばあちゃんへのメッセージ
   - 「機器管理」: 炊飯器のロック/解除
5. **LINEで「ヘルプ」と送る**ように説明 → コマンド一覧が来る
6. **公式アカウント友だち登録**を確認（QRコード: LINE Developersコンソール）

---

## ⏰ 設置後 24時間の監視ポイント

### 1時間後
- [ ] LINEに「サーバが落ちた」通知が来ていない（health_check）
- [ ] イベントがDBに溜まっている

### 6時間後
- [ ] 食事検知の精度を確認（誤検知が多くないか）
- [ ] お花が育っているか
- [ ] スタンプが押されているか

### 24時間後
- [ ] 「1日のまとめ」LINEが22時に届いた
- [ ] 翌3時のDBバックアップが完了している
- [ ] 異常検知の誤発火がないか確認

```bash
# 24時間後の総点検
sudo journalctl --since "24 hours ago" -u iot-monitor | grep -i error
ls -la data/backup/  # 当日のバックアップ
ls -la data/anomaly_flags/  # 異常検知フラグ
```

---

## 🚨 トラブルシューティング

### ラズパイに接続できない
```bash
# mDNSで接続
ssh tara0@[hostname].local

# mDNS失敗時
nmap -sn 192.168.x.0/24  # ルーター管理画面でIP確認も可
```

### 炊飯器のP110Mが応答しない
```bash
# Matter再ペアリングが必要な場合
sudo systemctl restart iot-matter
sleep 10
python scripts/test_p110m.py

# それでもダメならTapoアプリから削除→再ペアリング
```

### カメラが映らない
- Tapoアプリでカメラアカウントのパスワードを再設定
- `.env` の `CAMERA_USERNAME` / `CAMERA_PASSWORD` を更新
- `sudo systemctl restart iot-monitor`

### タブレットが表示されない
- Wi-Fiが同じSSIDか確認
- `http://[ラズパイIP]:8000/tablet` を直接試す
- ラズパイの IP が変わった可能性 → mDNS で `[hostname].local` を試す

### LINEから「リンク」を送っても返信がない
1. LINE Developers コンソールで Webhook が **オン**になっているか
2. Webhook URL が現在のtrycloudflare.com URLになっているか
3. `sudo journalctl -u iot-web -f` でwebhookログを確認
4. 緊急対処: `bash scripts/start_tunnel.sh` で再起動（webhook URLが再登録される）

### 家族からの誤通知が多い
`scripts/anomaly_check.py` の閾値を調整：
- `INACTIVITY_HOURS = 4` → 6など長めに
- `FRIDGE_OPEN_THRESHOLD_MIN = 30` → 60など長めに

---

## 🔄 ロールバック・撤収手順

### 一時停止（祖母が反発した場合）
```bash
# 全停止
sudo systemctl stop iot-matter iot-web iot-monitor

# タブレット画面を黒くする
# → タブレットの電源を切る
```

### データだけ取って撤収
```bash
# DBをUSBにコピー
cp data/iot.db /media/USB/iot_backup_$(date +%Y%m%d).db

# 全停止＋自動起動オフ
sudo systemctl stop iot-matter iot-web iot-monitor
sudo systemctl disable iot-matter iot-web iot-monitor
```

### 機器の物理撤去
1. P110Mを外す（炊飯器を直接コンセントへ）
2. T110をすべて取り外す
3. C220のケーブルを抜く
4. ラズパイの電源を抜く

---

## 📞 緊急連絡

設置中にトラブルがあった場合：
- 母にLINE
- LINE公式アカウントに「ヘルプ」を送ると一覧が見られる
- このドキュメントのトラブルシューティングを上から順に試す

---

## ✅ 設置完了の判定基準

以下が全てOKなら「Phase 1 投入完了」と宣言：
- [ ] systemdサービス全部 active
- [ ] LINEに「公開URL」通知が届いている
- [ ] タブレットが祖母の前に設置され、PWA起動
- [ ] 家族のスマホでLINE「状況」が応答する
- [ ] 全センサーが少なくとも1度は反応した
- [ ] 顔登録が祖母・母・祖父の3名分完了
- [ ] cronが登録されている（`crontab -l`で確認）
- [ ] 家族3名以上がLINE公式アカウントを友だち登録済み
