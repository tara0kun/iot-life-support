"""祖母用タブレットアプリ。

WebViewでラズパイ上のタブレットUIを全画面表示する。
"""
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.clock import Clock

import os

# WebViewはAndroid上でのみ動作
try:
    from android.runnable import run_on_ui_thread
    from jnius import autoclass

    WebView = autoclass("android.webkit.WebView")
    WebViewClient = autoclass("android.webkit.WebViewClient")
    WebSettings = autoclass("android.webkit.WebSettings")
    Activity = autoclass("org.kivy.android.PythonActivity")
    View = autoclass("android.view.View")
    WindowManager = autoclass("android.view.WindowManager$LayoutParams")
    ANDROID = True
except ImportError:
    ANDROID = False
    run_on_ui_thread = lambda f: f

# ラズパイのアドレス（実家Wi-Fiでは変わる可能性あり）
SERVER_URL = os.environ.get("IOT_SERVER_URL", "http://raspberrypi.local:8000/tablet")


class TabletApp(App):
    def build(self):
        layout = BoxLayout()
        self.title = "きょうの記録"
        Clock.schedule_once(self._start_webview, 0)
        return layout

    @run_on_ui_thread
    def _start_webview(self, *args):
        if not ANDROID:
            print(f"PC上での実行: ブラウザで {SERVER_URL} にアクセスしてください")
            return

        activity = Activity.mActivity

        # フルスクリーン
        window = activity.getWindow()
        window.addFlags(WindowManager.FLAG_FULLSCREEN)
        window.addFlags(WindowManager.FLAG_KEEP_SCREEN_ON)

        # ステータスバー・ナビゲーションバーを隠す
        decor = window.getDecorView()
        decor.setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
        )

        webview = WebView(activity)
        settings = webview.getSettings()
        settings.setJavaScriptEnabled(True)
        settings.setDomStorageEnabled(True)
        settings.setCacheMode(WebSettings.LOAD_DEFAULT)
        settings.setUseWideViewPort(True)
        settings.setLoadWithOverviewMode(True)

        webview.setWebViewClient(WebViewClient())
        webview.loadUrl(SERVER_URL)

        activity.setContentView(webview)

    def on_pause(self):
        return True

    def on_resume(self):
        pass


if __name__ == "__main__":
    TabletApp().run()
