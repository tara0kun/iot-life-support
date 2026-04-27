[app]
title = きょうの記録
package.name = kiroku
package.domain = org.iot

source.dir = .
source.include_exts = py

version = 1.0.0

requirements = python3,kivy,pyjnius,android

# WebView の動作とネットワーク通信に必要な権限
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK,ACCESS_WIFI_STATE

# このアプリをホームランチャー化する
# CATEGORY_HOME / CATEGORY_DEFAULT を MAIN intent に追加
# → ホームボタンを押すと本アプリに戻ってくる
android.manifest.intent_filters = filters.xml

# その他のメタデータ
android.meta_data = android.app.lib_name=python

# 縦横自動 (祖母タブレットの設置に応じる)
orientation = sensor
fullscreen = 1

android.api = 33
android.minapi = 21
android.ndk_api = 21
android.archs = arm64-v8a, armeabi-v7a

# アプリの起動カテゴリ（buildozer は launcher の登録もここで指定可）
android.entrypoint = org.kivy.android.PythonActivity

# アイコン・スプラッシュ（用意できたら有効化）
# icon.filename = icon.png
# presplash.filename = splash.png

log_level = 2

[buildozer]
warn_on_root = 1
