#!/bin/bash
# systemdサービスと、ホスト側の接続安定化設定のインストール
# 使い方: sudo bash systemd/install.sh
#
# サービスだけ入れたいとき:   sudo bash systemd/install.sh --services-only
# ホスト設定だけ入れたいとき: sudo bash systemd/install.sh --host-only

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"

MODE="${1:-all}"

install_services() {
    for f in iot-matter.service iot-web.service iot-monitor.service; do
        cp "$DIR/$f" /etc/systemd/system/
        echo "  Installed $f"
    done
    systemctl daemon-reload
    systemctl enable iot-matter iot-web iot-monitor
    echo "  サービスの再起動: sudo systemctl restart iot-matter iot-web iot-monitor"
}

install_host_config() {
    # Wi-Fi の省電力を切る。**これが on だと SSH が作業中に切れる。**
    # 2026-10-24 の実測で power_save=on、Tailscale の経路張り替えが2日に77件。
    if [ -d /etc/NetworkManager/conf.d ]; then
        cp "$ROOT/config/NetworkManager/wifi-powersave-off.conf" \
           /etc/NetworkManager/conf.d/
        echo "  Installed wifi-powersave-off.conf"
        systemctl reload NetworkManager 2>/dev/null \
            || systemctl restart NetworkManager
        # 設定ファイルは次の接続から効く。今つながっている分には即時反映させる。
        for dev in $(nmcli -t -f DEVICE,TYPE device 2>/dev/null \
                     | awk -F: '$2=="wifi"{print $1}'); do
            iw dev "$dev" set power_save off 2>/dev/null \
                && echo "    $dev: power_save off（即時反映）"
        done
    else
        echo "  NetworkManager が無いので Wi-Fi 設定はスキップ"
    fi

    # SSH の生存確認。切れた接続を掴み続けないようにする。
    if [ -d /etc/ssh/sshd_config.d ]; then
        cp "$ROOT/config/ssh/60-keepalive.conf" /etc/ssh/sshd_config.d/
        echo "  Installed 60-keepalive.conf"
        # **壊れた設定で reload しない。** 構文が通ることを確認してから入れ替える。
        if sshd -t 2>/dev/null; then
            systemctl reload ssh 2>/dev/null || systemctl reload sshd
            echo "    sshd を reload した"
        else
            echo "    ⚠️ sshd の設定に問題があるので reload しない:"
            sshd -t || true
            rm -f /etc/ssh/sshd_config.d/60-keepalive.conf
            echo "    入れた設定を取り消した"
            return 1
        fi
    else
        echo "  sshd_config.d が無いので SSH 設定はスキップ"
    fi
}

verify() {
    echo
    echo "=== 確認 ==="
    for dev in $(nmcli -t -f DEVICE,TYPE device 2>/dev/null \
                 | awk -F: '$2=="wifi"{print $1}'); do
        echo "  $dev: $(iw dev "$dev" get power_save 2>/dev/null)"
    done
    sshd -T 2>/dev/null | grep -E '^(clientaliveinterval|clientalivecountmax|tcpkeepalive)' \
        | sed 's/^/  /'
}

case "$MODE" in
    --services-only) install_services ;;
    --host-only)     install_host_config; verify ;;
    all|*)           install_services; install_host_config; verify ;;
esac

echo
echo "Done."
