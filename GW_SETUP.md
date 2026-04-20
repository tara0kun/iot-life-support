# GW設置手順書（2026-04-29〜05-06）

祖母宅でのIoTシステム設置手順。上から順にやれば完了する。

---

## 事前準備（自宅で完了させておく）

- [x] ラズパイにすべてのコード・設定がインストール済み
- [x] systemdサービス登録済み（iot-matter, iot-web, iot-monitor）
- [x] 祖母宅Wi-Fi事前登録スクリプト（setup_grandma_wifi.sh）
- [ ] タブレット端末を用意（Android推奨、10インチ程度）
- [ ] 延長コード（必要なら）
- [ ] 両面テープ/マジックテープ（T110固定用）
- [ ] カメラ固定用のフック or スタンド

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

祖母宅Wi-Fi情報:
- SSID: `[GRANDMA_WIFI_SSIDの値]`（2.4GHz）
- PW: `[GRANDMA_WIFI_PASSの値]`

---

## Step 2: ラズパイ設置（5分）

1. ルーター付近にラズパイを設置（電源確保）
2. 電源投入
3. systemdサービスが自動起動するのを待つ（約1分）

```bash
# サービス確認
sudo systemctl status iot-matter iot-web iot-monitor
```

---

## Step 3: P110M（炊飯器用スマートプラグ）設置（10分）

1. 炊飯器のプラグを抜く
2. P110Mをコンセントに差す
3. 炊飯器のプラグをP110Mに差す
4. Tapoアプリで同じWi-Fiに接続されていることを確認

```bash
# 電力読み取りテスト
source venv/bin/activate
python scripts/test_p110m.py
```

期待値:
- 待機中: 0〜5W
- 炊飯中: 600W以上（しきい値: 100W）

**もし炊飯器のW数が想定と大きく違う場合:**
```bash
# .envのしきい値を調整
nano .env
# RICE_COOKER_THRESHOLD_W=100 を適切な値に変更
```

---

## Step 4: T110（冷蔵庫ドアセンサー）設置（10分）

1. H100ハブの電源を入れる（ルーター付近がベスト）
2. T110のフィルムを剥がして起動
3. 冷蔵庫の観音開きドアの**よく使う側**に両面テープで貼付
   - 本体: ドア側
   - マグネット: 本体側（フレーム側）
4. 開閉でLEDが光ることを確認

```bash
# H100経由で認識確認
python -c "
from kasa import Discover
import asyncio
async def check():
    dev = await Discover.discover_single('$HUB_IP', username='[TAPO_USERNAMEの値]', password='[TAPO_PASSWORDの値]')
    await dev.update()
    for child in dev.children:
        print(f'{child.alias}: {child.device_id}')
asyncio.run(check())
"
```

---

## Step 5: T110（浴室ドアセンサー）設置（5分）

1. T110を浴室ドアの枠に両面テープで貼付
2. Tapoアプリでエイリアスを「浴室ドア」にリネーム（重要！monitor.pyが名前で判別）

---

## Step 6: T100（脱衣所モーションセンサー）設置（5分）

1. T100を脱衣所の棚や壁に設置（動きを検知しやすい位置）
2. Tapoアプリでエイリアスを「脱衣所」にリネーム

---

## Step 7: C220カメラ設置（15分）

1. 壁時計の横（写真3129-3130で確認した位置）にフックで設置
2. キッチン全体が映る角度に調整
3. 電源ケーブルを壁沿いに配線
4. Tapoアプリで映像確認

```bash
# RTSP接続テスト
python scripts/test_camera.py
```

設置候補: 壁時計の横（キッチン全体を見渡せる高い位置）

---

## Step 8: 祖母の顔登録（5分）

祖母がキッチンにいるときに実行:
```bash
source venv/bin/activate
python scripts/register_face.py --person-id 1 --name 祖母
```

カメラの前に立ってもらい、数秒待つ。
「顔を検出しました」と出れば成功。

母の顔も登録:
```bash
python scripts/register_face.py --person-id 2 --name 母
```

---

## Step 9: タブレット設置（10分）

1. タブレットを祖母宅Wi-Fiに接続
2. ブラウザで `http://[hostname].local:8000/tablet` を開く
3. 「ホーム画面に追加」でPWAとして追加
4. テーブルの上に立てかける（写真で確認した位置）
5. 充電ケーブルを接続

祖母への説明:
> 「これはおばあちゃんの記録帳だよ。今日なにをしたか勝手に記録されるの。お花も育つよ。」

---

## Step 10: 動作確認テスト（15分）

### 炊飯器テスト
1. 炊飯器の電源を入れる
2. タブレットに「朝食」（時間帯による）が表示されることを確認
3. P110Mの電力値が正しく取得されていることを確認

### 冷蔵庫テスト
1. 冷蔵庫のドアを開閉する
2. ログにイベントが記録されることを確認

### お風呂テスト
1. 浴室のドアを閉めて開ける
2. タブレットに「お風呂」が表示されることを確認

### LINE通知テスト
```bash
source venv/bin/activate
python -c "from src.notifier import send_line_message; send_line_message('設置完了テスト: IoTシステムが正常に動作しています')"
```

### 全体確認
```bash
# ログ確認
sudo journalctl -u iot-monitor -f

# DB確認
python -c "
from src.db import get_conn
conn = get_conn()
for r in conn.execute('SELECT * FROM events ORDER BY id DESC LIMIT 5').fetchall():
    print(dict(r))
conn.close()
"
```

---

## Step 11: 家族のスマホ設定（5分）

1. 母のスマホで `http://[hostname].local:8000/family` を開く
2. パスワード `[FAMILY_PASSWORDの値]` でログイン
3. ブックマーク追加
4. 「かんたん記録」の使い方を説明:
   > おばあちゃんが「薬飲んだよ」と言ったら、ここのボタンを押してね

---

## トラブルシューティング

### ラズパイに接続できない
```bash
# mDNSで接続
ssh tara0@[hostname].local

# mDNSが効かない場合、ルーターの管理画面でIPを確認
# または nmap でスキャン
nmap -sn 192.168.x.0/24
```

### P110Mが応答しない
```bash
# Matterサーバ再起動
sudo systemctl restart iot-matter
sleep 10
python scripts/test_p110m.py
```

### カメラが映らない
- Tapoアプリでカメラアカウントのパスワードを確認
- `.env`の`CAMERA_USERNAME`/`CAMERA_PASSWORD`を更新

### タブレットが表示されない
- Wi-Fiが同じSSIDか確認
- `http://[ラズパイのIP]:8000/tablet` を直接試す

---

## 撤収時の注意

GW終了後も継続運用の場合:
- 電源ケーブルを踏まない位置に固定
- ラズパイは触らない場所に設置
- カメラのケーブルが目立たないように配線

中断する場合:
```bash
sudo systemctl stop iot-matter iot-web iot-monitor
sudo systemctl disable iot-matter iot-web iot-monitor
```
