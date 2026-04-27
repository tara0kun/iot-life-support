# 祖母用タブレット キオスクアプリ

Androidタブレットでラズパイ上のタブレットUI（`/tablet`）をキオスクモードで表示するアプリ。

## 仕組み

- **Kivy + WebView** でラズパイのタブレットUIを全画面表示
- **本アプリをホームランチャー化**することでホームボタンを押しても本アプリに戻る
- Immersive Sticky Modeでステータスバー・ナビゲーションバーを隠蔽
- WebView の状態を60秒ごとにチェック、エラー時は自動再ロード
- 画面常時ON（FLAG_KEEP_SCREEN_ON）

## ビルド方法

### 方法A: GitHub Actions（推奨）⭐

1. GitHub にプッシュすると `.github/workflows/build-apk.yml` が自動で動作
2. Actions タブ → 該当 workflow run → **Artifacts** に `kiroku-apk` がアップロードされる
3. ダウンロードしてタブレットに転送

URLをカスタマイズしたい場合は `Run workflow` から「IoTサーバURL」を指定して実行。

### 方法B: ローカルビルド

#### 必要環境
- Linux（Ubuntu 22.04 推奨。WSLでも可）
- Python 3.11
- JDK 17
- buildozer

#### 手順
```bash
# 依存
sudo apt update
sudo apt install -y build-essential git zip unzip openjdk-17-jdk \
                    autoconf libtool pkg-config zlib1g-dev \
                    libncurses5-dev libncursesw5-dev libtinfo5 \
                    cmake libffi-dev libssl-dev

# Python依存
pip install buildozer cython==0.29.36

# ビルド
cd android
buildozer android debug

# bin/kiroku-1.0.0-arm64-v8a-debug.apk が生成される
```

初回ビルドはAndroid SDK/NDKをダウンロードするため30分〜1時間かかる。
2回目以降はキャッシュにより数分〜10分程度。

## URL設定

接続先URL（ラズパイ）は3通りで設定できる：

### 1. ビルド時に固定（main.py の DEFAULT_URL を編集）
```python
DEFAULT_URL = "http://taraberrypi.local:8000/tablet"
```

### 2. GitHub Actions の workflow_dispatch で指定
GitHubの Actions 画面で `Run workflow` → 「IoTサーバURL」入力

### 3. インストール後にファイルで上書き（再ビルド不要）
ADB等で以下にファイル設置：
```
/sdcard/Android/data/org.iot.kiroku/files/url.txt
```
中身: 1行で接続先URL（例: `http://192.168.1.42:8000/tablet?token=xxx`）

## インストール手順

1. タブレットの **設定 → セキュリティ → 提供元不明のアプリのインストール** を許可
2. APKをタブレットに転送（USB / Google Drive / メール添付など）
3. ファイルマネージャでAPKをタップ → インストール
4. 「きょうの記録」アプリを起動
5. **ホームボタンを押す → 「常にこのアプリを使用」を選択**
   - これでランチャー化完了。電源ONや再起動後も自動起動

## メンテナンス（家族・開発者）

このアプリには **PIN脱出機能や設定UIはありません**（祖母用に絞った設計）。
メンテナンスが必要な場合は以下のいずれか：

### 別ランチャーに切り替える
1. Android **設定 → アプリ → デフォルトのアプリ → ホームアプリ**
2. 別のランチャー（例: Pixel Launcher、Nova Launcher 等）を選択
3. メンテナンス完了後、再び「きょうの記録」を選択して戻す

### ADB経由でアプリを停止
PC から:
```bash
adb shell am force-stop org.iot.kiroku
adb shell pm clear org.iot.kiroku  # 設定もリセットしたい場合
```

### アンインストール
通常のアプリと同様に：設定 → アプリ → きょうの記録 → アンインストール

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| 起動時に真っ白 | Wi-Fi接続を確認、ラズパイの`iot-web`が起動しているか確認 |
| ネットワークエラー | URLの`taraberrypi.local`が解決できるか確認、IP直指定に変更 |
| 音声が出ない | `MediaPlaybackRequiresUserGesture=false` 設定済。タブレット側の音量を確認 |
| 自動起動しない | ホームアプリのデフォルト設定を再確認 |
| 画面が消える | `KEEP_SCREEN_ON` 設定済。タブレット側のスリープ時間も「無し」に設定推奨 |

## 既知の制約

- **電源ボタン**は OS レベルなので無効化不可
- **緊急 SOS** など OS が割り込む機能は無効化不可
- **再起動時に自動起動するには** ホームランチャー設定が必要
- 祖母が誤操作で別アプリを起動したくても、ホームランチャー設定により次のホームボタン押下で戻る
