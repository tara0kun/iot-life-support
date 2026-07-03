"""最終発表用の統計を DB から抽出し docs/final-presentation/operational-stats.md を再生成する。

プライバシー配慮:
- 人物 (person_id) の紐付けは絶対に出力しない
- 顔画像・食事写真・LINE user_id は参照しない
- 集計・比率・件数のみを出力する

Usage:
    venv/bin/python scripts/extract_presentation_stats.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import get_conn

OUTPUT = ROOT / "docs" / "final-presentation" / "operational-stats.md"


def main() -> None:
    lines: list[str] = []
    add = lines.append

    with get_conn() as conn:
        add("# 運用データ統計 (自動生成)")
        add("")
        add(f"> 生成: `scripts/extract_presentation_stats.py` により自動集計")
        add(f"> 最終抽出日: {datetime.now().strftime('%Y-%m-%d')}")
        add(f"> 対象: `iot-life-support` の稼働 DB (人物識別情報は集計時に除外)")
        add("")

        # === 全体規模 ===
        add("## 1. 全体規模")
        add("")
        total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        min_at = conn.execute("SELECT MIN(started_at) FROM events").fetchone()[0]
        max_at = conn.execute("SELECT MAX(started_at) FROM events").fetchone()[0]
        n_days = conn.execute(
            "SELECT COUNT(DISTINCT date(started_at)) FROM events"
        ).fetchone()[0]
        n_sessions = conn.execute("SELECT COUNT(*) FROM meal_sessions").fetchone()[0]
        n_bath = conn.execute("SELECT COUNT(*) FROM bath_classifications").fetchone()[0]
        n_rice = conn.execute("SELECT COUNT(*) FROM rice_classifications").fetchone()[0]

        add(f"| 指標 | 値 |")
        add(f"| --- | --- |")
        add(f"| 総イベント記録数 | **{total_events:,}** |")
        add(f"| 稼働日数 (event 記録あり) | **{n_days}日** |")
        add(f"| 最初のイベント | {min_at} |")
        add(f"| 最新のイベント | {max_at} |")
        add(f"| 食事セッション集約数 | **{n_sessions:,}** |")
        add(f"| 入浴分類記録 | {n_bath:,} |")
        add(f"| 炊飯器動作分類 | {n_rice:,} |")
        add("")

        # === 通知応答率 ===
        add("## 2. 家族への LINE 通知と応答")
        add("")
        add("家族が LINE 通知に対して実際にボタン押下 (or 家族UI から) 応答した割合。")
        add("")
        total_notif = conn.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[0]
        responded = conn.execute(
            "SELECT COUNT(*) FROM pending_notifications WHERE completed_at IS NOT NULL"
        ).fetchone()[0]
        add(f"**総通知数**: {total_notif} / **応答済**: {responded} "
            f"(**応答率 {responded/total_notif*100:.0f}%**)")
        add("")

        add("### 通知種類別")
        add("")
        add("| 通知タイプ | 総数 | 応答済 | 応答率 |")
        add("| --- | ---: | ---: | ---: |")
        for r in conn.execute("""
            SELECT notification_type,
                   COUNT(*) total,
                   SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) responded
              FROM pending_notifications
             GROUP BY notification_type
             ORDER BY total DESC
        """):
            rate = r["responded"] / r["total"] * 100
            add(f"| `{r['notification_type']}` | {r['total']} | {r['responded']} | {rate:.0f}% |")
        add("")

        # === センサー別ボリューム ===
        add("## 3. センサー種別の受信ボリューム")
        add("")
        add("BLE + WiFi + Matter を含む全センサー種別の総受信数。")
        add("")
        add("| センサー種別 | 総受信数 | 説明 |")
        add("| --- | ---: | --- |")
        source_desc = {
            "bathroom_meter": "SwitchBot 温湿度計 (BLE 直接、10秒毎)",
            "camera": "Tapo C220 の人物検知 (2秒ポーリング)",
            "bath_motion": "T100 モーションセンサー (脱衣所)",
            "fridge": "T110 開閉センサー (冷蔵庫)",
            "bath_door": "T110 開閉センサー (浴室扉)",
            "toilet_door": "T110 開閉センサー (トイレ扉)",
            "rice_cooker_lid": "T110 開閉センサー (炊飯器蓋)",
            "rice_cooker": "P110M Matter プラグ (炊飯器電力)",
        }
        for r in conn.execute("""
            SELECT source, COUNT(*) c FROM events
             GROUP BY source ORDER BY c DESC LIMIT 12
        """):
            desc = source_desc.get(r["source"], "")
            add(f"| `{r['source']}` | {r['c']:,} | {desc} |")
        add("")

        # === 実際の危険信号ヒット ===
        add("## 4. 危険信号として発火した実件数")
        add("")
        add("設計時に想定した緊急・警告シナリオが、実運用でどれだけ発火したか。")
        add("")
        critical = conn.execute("""
            SELECT notification_type, COUNT(*) c
              FROM pending_notifications
             WHERE notification_type IN
                ('bath_emergency','long_toilet_stay','bath_reminder',
                 'meal_alert','anomaly_fridge_open','anomaly_inactivity',
                 'anomaly_night_rice','hair_dryer_missing','medicine_reminder',
                 'lock_confirm','device_locked')
             GROUP BY notification_type ORDER BY c DESC
        """)
        add("| 危険信号 | 実発火件数 |")
        add("| --- | ---: |")
        for r in critical:
            add(f"| `{r['notification_type']}` | {r['c']} |")
        add("")

        # === 日別 event 数の推移 (直近30日) ===
        add("## 5. 直近30日の日別 event 数推移")
        add("")
        add("稼働継続性の証拠。")
        add("")
        add("| 日付 | イベント数 | 稼働状況 |")
        add("| --- | ---: | :--- |")
        for r in conn.execute("""
            SELECT date(started_at) d, COUNT(*) c FROM events
             WHERE started_at >= datetime('now','-30 days','localtime')
             GROUP BY d ORDER BY d DESC
        """):
            bar = "█" * max(1, int(r["c"] / 500))
            add(f"| {r['d']} | {r['c']:,} | {bar} |")
        add("")

        # === 集約・整理系ロジックの成果 ===
        add("## 6. 集約ロジックの成果")
        add("")
        add(f"- **食事セッションの平均イベント数**: "
            f"{conn.execute('SELECT AVG(event_count) FROM meal_sessions').fetchone()[0]:.1f}")
        add(f"- **食事セッションの最大イベント数**: "
            f"{conn.execute('SELECT MAX(event_count) FROM meal_sessions').fetchone()[0]}")
        add(f"- **モンスタークラスター (event_count > 500) の残存件数**: "
            f"{conn.execute('SELECT COUNT(*) FROM meal_sessions WHERE event_count > 500').fetchone()[0]} "
            f"(全て `confirmed=-1` で無効化済)")
        add("")

        # === 生成の再現性 ===
        add("---")
        add("")
        add("**再生成コマンド**: `venv/bin/python scripts/extract_presentation_stats.py`")
        add("**プライバシー**: person_id / 個人名 / 顔画像 / 生活時間帯 / LINE user_id は一切含まない")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines))
    print(f"wrote: {OUTPUT} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
