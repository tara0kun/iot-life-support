#!/bin/bash
# Cloudflare Quick Tunnel を起動し、公開URLをLINEに通知する
# URLは再起動ごとに変わるが、LINEで家族に自動通知される

CLOUDFLARED="$HOME/cloudflared"
IOT_DIR="$HOME/IoT"
LOG_FILE="$IOT_DIR/logs/tunnel.log"
URL_FILE="$IOT_DIR/data/tunnel_url.txt"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$URL_FILE")"

# 既存のトンネルを停止
pkill -f "cloudflared tunnel" 2>/dev/null
sleep 2

# トンネル起動（バックグラウンド）
$CLOUDFLARED tunnel --url http://localhost:8000 > "$LOG_FILE" 2>&1 &
TUNNEL_PID=$!
echo "Tunnel PID: $TUNNEL_PID"

# URLが発行されるまで待機（最大30秒）
for i in $(seq 1 30); do
    URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -1)
    if [ -n "$URL" ]; then
        echo "$URL" > "$URL_FILE"
        echo "公開URL: $URL"
        echo "タブレット: $URL/tablet"
        echo "家族画面:   $URL/family"

        # トークン読み込み
        TABLET_TOKEN=$(grep "^TABLET_TOKEN=" "$IOT_DIR/.env" 2>/dev/null | cut -d= -f2)

        # LINEに通知
        cd "$IOT_DIR"
        source venv/bin/activate
        python -c "
from src.notifier import send_line_message
url = '$URL'
token = '$TABLET_TOKEN'
tablet_url = f'{url}/tablet?token={token}' if token else f'{url}/tablet'
msg = f'''🌐 IoTシステムの公開URLが更新されました

📱 タブレット画面:
{tablet_url}

👨‍👩‍👧 家族管理画面:
{url}/family
パスワード: .envのFAMILY_PASSWORDを確認

このURLをブックマークしてください。'''
send_line_message(msg)
print('LINE通知送信完了')
"
        exit 0
    fi
    sleep 1
done

echo "ERROR: URLの発行に失敗しました"
cat "$LOG_FILE"
exit 1
