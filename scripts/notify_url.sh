#!/bin/bash
# 現在の公開URLをLINEに通知する
# iot-web再起動時にsystemd ExecStartPostから呼ばれる

IOT_DIR="$HOME/IoT"
URL_FILE="$IOT_DIR/data/tunnel_url.txt"

# URLファイルがなければトンネル未起動
if [ ! -f "$URL_FILE" ]; then
    echo "トンネルURL未発行 → スキップ"
    exit 0
fi

URL=$(cat "$URL_FILE" 2>/dev/null)
if [ -z "$URL" ]; then
    echo "URLが空 → スキップ"
    exit 0
fi

# トークン読み込み
TABLET_TOKEN=$(grep "^TABLET_TOKEN=" "$IOT_DIR/.env" 2>/dev/null | cut -d= -f2)

cd "$IOT_DIR"
source venv/bin/activate
python -c "
from src.notifier import send_line_message
url = '$URL'
token = '$TABLET_TOKEN'
tablet_url = f'{url}/tablet?token={token}' if token else f'{url}/tablet'
msg = f'''🔄 Webサーバが再起動しました

📱 タブレット画面:
{tablet_url}

👨‍👩‍👧 家族管理画面:
{url}/family'''
send_line_message(msg)
print('URL通知送信完了')
" 2>&1
