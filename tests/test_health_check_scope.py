"""健全性チェックが「何を異常とみなすか」を固定する。

**壊れた窓を放置しない。** 2026-10-24 時点で、`sensor-activity` は
`tablet_report` だけを理由に恒久的に ❌ になっていた（最終記録 2026-07-13、
102日間ゼロ）。常に ❌ だと、本物のセンサーが死んだときに状態変化として
目立たなくなる。

ボタンが押されないのは**利用状況**であって健全性の問題ではない。端末自体は
生きている（/tablet が1日約290回読まれている）。分類違いだったので外した。

逆に、物理センサーをここから落とすと死活監視が穴だらけになる。
どちらの向きの間違いも、このファイルで止める。
"""
from __future__ import annotations

import inspect
import re

import scripts.health_check as hc


def _watched() -> dict[str, int]:
    """check_recent_events 内の watched リストをソースから読む。

    関数内のローカル変数なので import では取れない。ソースを読んで
    (source, 許容時間) を抜き出す。
    """
    src = inspect.getsource(hc.check_recent_events)
    body = src.split("watched = [", 1)[1].split("]", 1)[0]
    out: dict[str, int] = {}
    for m in re.finditer(r'\(\s*"([a-z_]+)"\s*,\s*(\d+)\s*,', body):
        out[m.group(1)] = int(m.group(2))
    return out


def test_タブレットの利用状況を異常扱いしない():
    """これを戻すと sensor-activity が恒久的に ❌ になる。"""
    assert "tablet_report" not in _watched()


def test_物理センサーは全部監視されている():
    """ここから落ちると、そのセンサーが死んでも誰も気づけない。"""
    watched = _watched()
    for source in (
        "camera", "bathroom_meter", "fridge", "bath_door",
        "bath_motion", "toilet_door", "rice_cooker_lid", "rice_cooker",
    ):
        assert source in watched, f"{source} が監視対象から外れている"


def test_しきい値がセンサーの性質に合っている():
    """常時反応するものと、使ったときだけ反応するものを混ぜない。

    まとめて MAX で見ていた頃は、カメラが動いている限り残りが何日
    死んでいても ✅ を返していた（2026-09-04〜09-15 の11日間が実例）。
    """
    w = _watched()
    # 常時反応する系は短く
    assert w["camera"] <= 3
    assert w["bathroom_meter"] <= 3
    # 使ったときだけ反応する系はそれより長く
    for source in ("fridge", "bath_door", "bath_motion", "toilet_door"):
        assert w[source] > w["camera"], source
    # 1日1〜2回のものはさらに長く
    for source in ("rice_cooker_lid", "rice_cooker"):
        assert w[source] >= w["fridge"], source


def test_夜間は判定不能を返す():
    """夜間に ✅ を返すと、翌朝に嘘の「復旧しました」が飛ぶ。

    以前それで「センサーが壊れたまま毎晩22時に復旧通知」が送られていた。
    None は「状態を更新も通知もしない」を意味する。
    """
    src = inspect.getsource(hc.check_recent_events)
    assert "return None" in src, "夜間の判定不能パスが無い"
    assert re.search(r"7\s*<=\s*now\.hour\s*<\s*22", src), "日中の判定条件が見当たらない"
