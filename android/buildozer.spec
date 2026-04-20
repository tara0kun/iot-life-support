[app]
title = きょうの記録
package.name = kiroku
package.domain = org.iot

source.dir = .
source.include_exts = py

version = 1.0.0

requirements = python3,kivy,pyjnius,android

android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK

orientation = landscape
fullscreen = 1

android.api = 33
android.minapi = 21
android.ndk_api = 21
android.archs = arm64-v8a

# アイコン（後で差し替え可能）
# icon.filename = icon.png

# スプラッシュ画面
# presplash.filename = splash.png

# ログ
log_level = 2

[buildozer]
warn_on_root = 1
