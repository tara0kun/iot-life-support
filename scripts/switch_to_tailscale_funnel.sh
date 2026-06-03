#!/bin/bash
# Cloudflare Quick Tunnel から Tailscale Funnel へ切替
# 固定 URL: https://tara0.taile9fa63.ts.net
#
# 使い方: sudo bash scripts/switch_to_tailscale_funnel.sh
set -e

URL="https://tara0.taile9fa63.ts.net"
IOT_DIR="/home/tara0/IoT"

if [ "$EUID" -ne 0 ]; then
    echo "❌ sudo で実行してください: sudo bash $0"
    exit 1
fi

echo "==========================================="
echo "Tailscale Funnel への切替"
echo "新URL: $URL"
echo "==========================================="
echo ""

echo "=== Step 1: Tailscale Funnel 有効化 (port 8000) ==="
tailscale funnel --bg 8000
echo ""

echo "=== Step 2: Funnel 状態確認 ==="
tailscale funnel status
echo ""

echo "=== Step 3: 外部疎通テスト (最大15秒) ==="
ok=0
for i in $(seq 1 5); do
    if curl -sf --max-time 5 "$URL/" -o /dev/null; then
        echo "  ✅ $URL 応答OK (${i}/5)"
        ok=1
        break
    fi
    echo "  ... 試行 $i/5 (証明書発行に少し時間がかかる場合あり)"
    sleep 3
done
if [ $ok -eq 0 ]; then
    echo "  ⚠️ まだ応答なし。Tailscale 管理画面で Funnel が有効か確認してください:"
    echo "      https://login.tailscale.com/admin/settings/features"
    echo "  続行しますか？ (この時点では cloudflared はまだ生きているので元に戻せます)"
    read -p "  続行 [y/N]: " yn
    [ "$yn" = "y" ] || { echo "中止。tailscale funnel reset で元に戻せます"; exit 1; }
fi
echo ""

echo "=== Step 4: data/tunnel_url.txt 更新 ==="
echo "$URL" > "$IOT_DIR/data/tunnel_url.txt"
chown tara0:tara0 "$IOT_DIR/data/tunnel_url.txt"
cat "$IOT_DIR/data/tunnel_url.txt"
echo ""

echo "=== Step 5: LINE webhook URL 更新 ==="
sudo -u tara0 bash -c "cd $IOT_DIR && venv/bin/python -c \"
from src.notifier import update_webhook_url
ok = update_webhook_url('$URL/line/webhook')
print('LINE webhook 更新:', 'OK' if ok else 'NG')
\""
echo ""

echo "=== Step 6: cloudflared 停止 + 無効化 ==="
systemctl stop iot-tunnel
systemctl disable iot-tunnel
echo "  停止完了"
echo ""

echo "==========================================="
echo "✅ 切替完了"
echo ""
echo "今後の固定URL: $URL"
echo "  - 家族ページ : $URL/family"
echo "  - 祖母タブレット: $URL/tablet?token=<TABLET_TOKEN>"
echo "  - LINE webhook: $URL/line/webhook (自動更新済)"
echo ""
echo "祖母タブレットのブックマーク更新が必要です。"
echo "==========================================="
