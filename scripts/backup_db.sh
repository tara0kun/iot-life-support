#!/bin/bash
# DB を安全にバックアップ（Python sqlite3 .backup() で書き込み中も整合性保持）
# crontab 例: 0 3 * * * bash ~/IoT/scripts/backup_db.sh

IOT_DIR="$HOME/IoT"
SRC="$IOT_DIR/data/iot.db"
BACKUP_DIR="$IOT_DIR/data/backup"
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC が存在しません"
    exit 1
fi

DATE=$(date +%Y%m%d)
DEST="$BACKUP_DIR/iot_${DATE}.db"

# Python sqlite3 .backup() を使う（WAL対応・トランザクション安全）
cd "$IOT_DIR"
source venv/bin/activate

python -c "
import sqlite3, sys
src = sqlite3.connect('$SRC')
dst = sqlite3.connect('$DEST')
with dst:
    src.backup(dst)
src.close(); dst.close()
print('OK')
" 2>&1
RESULT=$?

if [ $RESULT -eq 0 ]; then
    SIZE=$(du -h "$DEST" | cut -f1)
    echo "backup OK: $DEST ($SIZE)"
else
    echo "ERROR: backup failed with code $RESULT"
    exit $RESULT
fi

# 古いバックアップ削除
find "$BACKUP_DIR" -name "iot_*.db" -mtime +$KEEP_DAYS -delete
echo "old backups (>$KEEP_DAYS days) cleaned"
