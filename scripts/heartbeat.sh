#!/bin/bash
# healthchecks.io に heartbeat を送信する。
# Pi 側 cron `*/5 * * * *` で登録して、15分無音で healthchecks.io 側が
# メール/LINE (Webhook) 通知する仕組み。
#
# 設定手順は HANDOFF.md 「外部死活監視」節を参照。
# .env に HEARTBEAT_URL=https://hc-ping.com/<UUID> を設定してから使う。

# .env 読込
set -a
[ -f "$HOME/IoT/.env" ] && . "$HOME/IoT/.env"
set +a

if [ -z "$HEARTBEAT_URL" ]; then
    exit 0   # 未設定なら何もしない (無効化状態)
fi

# サービス全部が active か確認、どれか死んでたら /fail エンドポイントを叩く
FAILED=""
for svc in iot-monitor iot-web iot-matter; do
    if ! systemctl is-active --quiet "$svc"; then
        FAILED="${FAILED}${svc},"
    fi
done

if [ -n "$FAILED" ]; then
    curl -fsS --max-time 5 --retry 2 -o /dev/null \
        "${HEARTBEAT_URL}/fail" \
        --data-raw "failed=${FAILED%,}" 2>&1
else
    curl -fsS --max-time 5 --retry 2 -o /dev/null "${HEARTBEAT_URL}" 2>&1
fi
