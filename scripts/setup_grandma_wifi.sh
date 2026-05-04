#!/bin/bash
# 現地Wi-Fiの事前登録
# 使い方: sudo bash scripts/setup_grandma_wifi.sh
#
# ラズパイが現地のWi-Fiに自動接続できるようにする。
# 自宅Wi-Fiも残すので、どちらの環境でも動作する。

set -e

# .envからWi-Fi情報を読み込み
ENV_FILE="$(dirname "$0")/../.env"
GRANDMA_SSID=$(grep "^GRANDMA_WIFI_SSID=" "$ENV_FILE" | cut -d= -f2)
GRANDMA_PASS=$(grep "^GRANDMA_WIFI_PASS=" "$ENV_FILE" | cut -d= -f2)

if [ -z "$GRANDMA_SSID" ] || [ -z "$GRANDMA_PASS" ]; then
    echo "ERROR: .envにGRANDMA_WIFI_SSID/GRANDMA_WIFI_PASSが設定されていません"
    exit 1
fi

# NetworkManager を使用（Raspberry Pi OS Bookworm以降）
if command -v nmcli &> /dev/null; then
    echo "NetworkManagerで現地Wi-Fiを追加..."
    nmcli connection add type wifi con-name "grandma-wifi" \
        wifi.ssid "$GRANDMA_SSID" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$GRANDMA_PASS" \
        connection.autoconnect yes \
        connection.autoconnect-priority 10
    echo "Done: $GRANDMA_SSID を追加しました（優先度10）"
    echo "自宅Wi-Fiより優先して接続されます（現地にいる場合）"
else
    # wpa_supplicant fallback
    echo "wpa_supplicantで現地Wi-Fiを追加..."
    WPA_FILE="/etc/wpa_supplicant/wpa_supplicant.conf"
    if grep -q "$GRANDMA_SSID" "$WPA_FILE" 2>/dev/null; then
        echo "既に登録されています"
        exit 0
    fi
    cat >> "$WPA_FILE" << EOF

network={
    ssid="$GRANDMA_SSID"
    psk="$GRANDMA_PASS"
    priority=10
}
EOF
    echo "Done: $WPA_FILE に追加しました"
fi
