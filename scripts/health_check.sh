#!/bin/bash
# ヘルスチェック: Webサーバが応答しなければLINE通知
# cronで5分おきに実行: */5 * * * * bash ~/IoT/scripts/health_check.sh

IOT_DIR="$HOME/IoT"
FLAG_FILE="$IOT_DIR/data/.alert_sent"

# Webサーバ応答チェック
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:8000/tablet 2>/dev/null)

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "303" ] || [ "$HTTP_CODE" = "403" ]; then
    # 正常 → フラグがあれば復旧通知
    if [ -f "$FLAG_FILE" ]; then
        cd "$IOT_DIR" && source venv/bin/activate
        python -c "
from src.notifier import send_line_message
send_line_message('✅ IoTシステムが復旧しました。正常に動作しています。')
"
        rm -f "$FLAG_FILE"
    fi
else
    # 異常 → まだ通知していなければ通知
    if [ ! -f "$FLAG_FILE" ]; then
        cd "$IOT_DIR" && source venv/bin/activate
        python -c "
from src.notifier import send_line_message
send_line_message('⚠️ IoTシステムに異常が発生しています。\nWebサーバが応答しません。\n確認をお願いします。')
"
        touch "$FLAG_FILE"
    fi
fi
