#!/bin/bash
# systemdサービスのインストール
# 使い方: sudo bash systemd/install.sh

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

for f in iot-matter.service iot-web.service iot-monitor.service; do
    cp "$DIR/$f" /etc/systemd/system/
    echo "  Installed $f"
done

systemctl daemon-reload
systemctl enable iot-matter iot-web iot-monitor
echo "Done. Start with: sudo systemctl start iot-matter iot-web iot-monitor"
