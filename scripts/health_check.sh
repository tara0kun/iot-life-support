#!/bin/bash
# health_check.py のラッパ（cron互換のため残置）
# 5分おきに実行して各コンポーネントの状態をチェック
IOT_DIR="$HOME/IoT"
cd "$IOT_DIR" || exit 1
source venv/bin/activate
exec python scripts/health_check.py
