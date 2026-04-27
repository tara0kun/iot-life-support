"""祖母用タブレット キオスクアプリ。

WebViewでラズパイ上のタブレットUIを全画面表示する。
ランチャーとして登録することで、ホームボタンを押しても本アプリに戻る。

メンテナンス（家族）:
  - APK 自体のアンインストール / 別ランチャー選択は通常の Android 操作で行う
  - 開発者は ADB で `am start -n <other launcher>` を使うか、設定から別ランチャーを選ぶ
"""
from __future__ import annotations

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.clock import Clock

import os
from pathlib import Path

# WebViewはAndroid上でのみ動作
try:
    from android.runnable import run_on_ui_thread
    from jnius import autoclass

    WebView = autoclass("android.webkit.WebView")
    WebViewClient = autoclass("android.webkit.WebViewClient")
    WebChromeClient = autoclass("android.webkit.WebChromeClient")
    WebSettings = autoclass("android.webkit.WebSettings")
    Activity = autoclass("org.kivy.android.PythonActivity")
    View = autoclass("android.view.View")
    WindowManager = autoclass("android.view.WindowManager$LayoutParams")
    ANDROID = True
except ImportError:
    ANDROID = False
    run_on_ui_thread = lambda f: f


# 接続先URL（ビルド時環境変数または下記デフォルト）
DEFAULT_URL = os.environ.get(
    "IOT_SERVER_URL", "http://taraberrypi.local:8000/tablet"
)
# /sdcard/Android/data/org.iot.kiroku/files/url.txt があればそちらを優先
CONFIG_DIR = "/sdcard/Android/data/org.iot.kiroku/files"


def _load_url() -> str:
    config = Path(CONFIG_DIR) / "url.txt"
    try:
        if config.exists():
            text = config.read_text().strip()
            if text:
                return text
    except Exception:
        pass
    return DEFAULT_URL


def _apply_immersive(window):
    decor = window.getDecorView()
    decor.setSystemUiVisibility(
        View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        | View.SYSTEM_UI_FLAG_FULLSCREEN
        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
    )


class TabletApp(App):
    def build(self):
        self.title = "きょうの記録"
        layout = BoxLayout()
        Clock.schedule_once(self._start_webview, 0)
        return layout

    @run_on_ui_thread
    def _start_webview(self, *args):
        if not ANDROID:
            print(f"PC上での実行: ブラウザで {_load_url()} にアクセス")
            return

        activity = Activity.mActivity
        self._activity = activity

        # ウィンドウフラグ
        window = activity.getWindow()
        window.addFlags(WindowManager.FLAG_FULLSCREEN)
        window.addFlags(WindowManager.FLAG_KEEP_SCREEN_ON)
        window.addFlags(WindowManager.FLAG_TURN_SCREEN_ON)
        window.addFlags(WindowManager.FLAG_DISMISS_KEYGUARD)

        _apply_immersive(window)

        # WebView
        webview = WebView(activity)
        self._webview = webview
        settings = webview.getSettings()
        settings.setJavaScriptEnabled(True)
        settings.setDomStorageEnabled(True)
        settings.setDatabaseEnabled(True)
        settings.setCacheMode(WebSettings.LOAD_DEFAULT)
        settings.setUseWideViewPort(True)
        settings.setLoadWithOverviewMode(True)
        # 音声読み上げ（Web Speech API）を許可
        settings.setMediaPlaybackRequiresUserGesture(False)
        # 混在コンテンツ許可（HTTPサーバへの接続）
        try:
            settings.setMixedContentMode(0)  # MIXED_CONTENT_ALWAYS_ALLOW
        except Exception:
            pass

        webview.setWebViewClient(WebViewClient())
        webview.setWebChromeClient(WebChromeClient())
        webview.loadUrl(_load_url())

        activity.setContentView(webview)

        # 60秒ごとに描画状態を確認、エラーなら再ロード
        Clock.schedule_interval(self._check_health, 60)

    def _check_health(self, dt):
        if not ANDROID or not hasattr(self, "_webview"):
            return
        try:
            url = self._webview.getUrl()
            # about:blank やエラー画面なら再読込
            if not url or str(url).startswith("about:"):
                self._webview.loadUrl(_load_url())
        except Exception:
            pass

    def on_pause(self):
        # バックグラウンド遷移を受理（OSが殺すのを防ぐ）
        return True

    def on_resume(self):
        if ANDROID and hasattr(self, "_activity"):
            try:
                _apply_immersive(self._activity.getWindow())
            except Exception:
                pass


if __name__ == "__main__":
    TabletApp().run()
