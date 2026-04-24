#!/bin/bash
# ログローテーション: logs/*.log を日次でgzip圧縮し、14日以上前は削除
# crontab 例: 0 4 * * *  bash ~/IoT/scripts/rotate_logs.sh

LOG_DIR="$HOME/IoT/logs"
KEEP_DAYS=14

cd "$LOG_DIR" || exit 1

DATE=$(date +%Y%m%d)

# 各ログを日付付きで圧縮（既に存在すればスキップ）
for f in monitor.log matter.log web.log tunnel.log cron.log health.log; do
    if [ ! -f "$f" ] || [ ! -s "$f" ]; then continue; fi
    archive="${f%.log}_${DATE}.log.gz"
    if [ -f "$archive" ]; then continue; fi
    gzip -c "$f" > "$archive"
    : > "$f"  # truncate原ファイル
    echo "rotated: $f → $archive"
done

# 14日以上前のアーカイブを削除
find "$LOG_DIR" -name "*.log.gz" -mtime +$KEEP_DAYS -delete

# data/captures/ も14日以上前は削除
find "$HOME/IoT/data/captures" -type f -mtime +$KEEP_DAYS -delete 2>/dev/null

echo "rotation complete ($(date))"
