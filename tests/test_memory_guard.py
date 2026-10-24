"""iot-monitor が膨らみすぎたときに再起動する判定（Issue #21）。

**なぜ cgroup ではないのか。** この Pi はファームウェアが
`cgroup_disable=memory` を注入しており、memory コントローラが無効。
unit の MemoryMax は設定値として見えるだけで強制されない
（`MemoryCurrent` が `[not set]` になる）。

実際に起きたこと（2026-10-23）:
    kernel: oom-kill: global_oom, task=python
    kernel: Out of memory: Killed process (python) anon-rss:1344288kB
    その6分後に Pi が再起動

殺された時点の RSS は 1344MB で、設定した上限 2GB を下回っていた。
システム全体が先に枯れるので、cgroup の上限では間に合わない。

しきい値を動かすと Pi が落ちるか、無駄な再起動が増えるかのどちらかになる。
ここで境界を固定する。
"""
from __future__ import annotations

import scripts.memory_guard as mg


def test_平常時は再起動しない():
    """実測の平常値（1000〜1150MB）で動かないこと。

    ここが誤発火すると、見守りが無駄に途切れる。
    """
    for rss in (900, 1015, 1083, 1150):
        assert mg.decide(rss, 1000) is None, rss


def test_しきい値を超えたら再起動する():
    reason = mg.decide(mg.RSS_LIMIT_MB, 1000)
    assert reason is not None
    assert "しきい値" in reason


def test_しきい値の1MB手前では再起動しない():
    assert mg.decide(mg.RSS_LIMIT_MB - 1, 1000) is None


def test_実際にOOMを起こした値では再起動する():
    """1344MB（2026-10-23 に kernel が kill した値）で必ず動くこと。

    この値を見逃す設定にしたら、同じ事故が再発する。
    """
    assert mg.decide(1344, 500) is not None


def test_システムが枯れかけていれば低いRSSでも動く():
    """OOM は誰かが要求した瞬間に起きる。枯れる前に手を打つ。"""
    reason = mg.decide(mg.RSS_FLOOR_MB + 50, mg.SYSTEM_AVAILABLE_FLOOR_MB - 50)
    assert reason is not None
    assert "空き" in reason


def test_主犯でないなら空きが少なくても再起動しない():
    """iot-monitor が小さいなら、再起動しても空きは戻らない。"""
    assert mg.decide(mg.RSS_FLOOR_MB - 100, 100) is None


def test_値が取れないときは何もしない():
    """計測できないことを理由に本番サービスを再起動しない。"""
    assert mg.decide(None, 1000) is None
    assert mg.decide(None, None) is None


def test_空きが不明でもRSSだけで判断できる():
    assert mg.decide(mg.RSS_LIMIT_MB + 1, None) is not None
    assert mg.decide(900, None) is None


def test_しきい値の並びが矛盾していない():
    """RSS_FLOOR は「主犯かどうか」の下限なので、上限より小さいこと。"""
    assert mg.RSS_FLOOR_MB < mg.RSS_LIMIT_MB
    # 平常値(〜1150MB)より上、OOM の 1344MB より下に上限を置く
    assert 1150 < mg.RSS_LIMIT_MB < 1344


def test_再起動の最短間隔が十分に長い():
    """短いと、起動直後の一時的な増加で再起動を繰り返す。"""
    assert mg.MIN_RESTART_INTERVAL_SECONDS >= 10 * 60
