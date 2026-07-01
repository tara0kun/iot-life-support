#!/bin/bash
# 週次システムスナップショット。
# 復旧に必要な最小限のもの (DB / 顔登録 / .env / systemd unit) を tar.gz にまとめ
# data/private/system_backups/ に保存。4週分だけ保持。
#
# cron: 0 4 * * 0  (毎週日曜 04:00)
#
# 復元手順:
#   cd /tmp && tar xzf /path/to/system_YYYYMMDD.tar.gz
#   (tar 内は絶対パスで戻せる形式)

set -e

BACKUP_DIR="/home/tara0/IoT/data/private/system_backups"
KEEP_WEEKS=4

mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d)
ARCHIVE="$BACKUP_DIR/system_${DATE}.tar.gz"

# 直近1時間の DB は WAL 反映のため checkpoint
sqlite3 /home/tara0/IoT/data/iot.db "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true

tar czf "$ARCHIVE" \
    /home/tara0/IoT/data/iot.db \
    /home/tara0/IoT/data/faces/known \
    /home/tara0/IoT/data/faces/encodings.json \
    /home/tara0/IoT/.env \
    /home/tara0/IoT/data/discovered_hub_ip.txt \
    /home/tara0/IoT/data/tunnel_url.txt \
    /etc/systemd/system/iot-monitor.service \
    /etc/systemd/system/iot-web.service \
    /etc/systemd/system/iot-matter.service \
    2>/dev/null

# 4週(28日)以上前のスナップショット削除
find "$BACKUP_DIR" -name "system_*.tar.gz" -mtime +$((KEEP_WEEKS * 7)) -delete

SIZE=$(du -h "$ARCHIVE" | cut -f1)
COUNT=$(find "$BACKUP_DIR" -name "system_*.tar.gz" | wc -l)
echo "snapshot: $(basename $ARCHIVE) ($SIZE), keeping $COUNT files"
