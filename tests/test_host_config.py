"""接続が切れないためのホスト設定を固定する。

作業中に SSH が切れる症状の原因を 2026-10-24 に調べた結果:

- Wi-Fi の省電力が on（`iw dev wlan0 get power_save` → Power save: on）
  wlan0 は DORMANT。チップがパケットの合間に寝るので、長時間つなぎっぱなしの
  TCP が落ちる
- `ClientAliveInterval` が 0（= サーバから生存確認を送らない）ので、
  回線が戻っても死んだ接続を掴み続ける
- Tailscale の経路張り替えが2日で77件、DERP 中継の張り替え24件
  （安定した回線ならこの頻度にはならない）

設定ファイルを消したり値を戻したりすると、この症状が再発する。
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NM_CONF = ROOT / "config" / "NetworkManager" / "wifi-powersave-off.conf"
SSH_CONF = ROOT / "config" / "ssh" / "60-keepalive.conf"
INSTALL = ROOT / "systemd" / "install.sh"


def _ssh_values() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in SSH_CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition(" ")
        out[key.lower()] = val.strip()
    return out


def test_wifiの省電力が無効化されている():
    """powersave=2 が disable。0 は default で、Raspberry Pi OS では on になる。"""
    p = configparser.ConfigParser()
    p.read(NM_CONF)
    assert p["connection"]["wifi.powersave"] == "2", (
        "0=default / 1=ignore / 2=disable / 3=enable。2 でなければ省電力が残る"
    )


def test_ssh生存確認が有効になっている():
    v = _ssh_values()
    assert v.get("tcpkeepalive") == "yes"
    interval = int(v["clientaliveinterval"])
    assert interval > 0, "0 だとサーバから生存確認を送らない（既定の壊れ方に戻る）"
    assert 10 <= interval <= 60, f"短すぎても無駄、長すぎると気づけない: {interval}"


def test_切断を許容する時間が現実的():
    """Wi-Fi の瞬断や Tailscale の経路張り替えは数秒で復帰する。
    短すぎると正常な瞬断で切られ、長すぎると死んだ接続が残る。
    """
    v = _ssh_values()
    total = int(v["clientaliveinterval"]) * int(v["clientalivecountmax"])
    assert 120 <= total <= 600, f"許容時間が {total}秒。2〜10分の範囲にする"


def test_installスクリプトが両方の設定を入れる():
    """片方だけ入れても症状は残る。"""
    text = INSTALL.read_text(encoding="utf-8")
    assert "wifi-powersave-off.conf" in text
    assert "60-keepalive.conf" in text


def test_installスクリプトは壊れたsshd設定でreloadしない():
    """構文を確認せずに reload すると、SSH で入れなくなる。

    実機は祖母宅にあり、SSH が死ぬと復旧に現地対応が必要になる。
    """
    text = INSTALL.read_text(encoding="utf-8")
    assert re.search(r"sshd\s+-t", text), "sshd -t による検証が無い"
    # 検証に失敗したら入れた設定を取り消すこと
    assert "rm -f /etc/ssh/sshd_config.d/60-keepalive.conf" in text


def test_installスクリプトはサービスとホスト設定を分けて実行できる():
    """サービスだけ入れ直したいときに、SSH 設定を触らせない。"""
    text = INSTALL.read_text(encoding="utf-8")
    assert "--services-only" in text
    assert "--host-only" in text

# --- memory_guard の権限（Issue #21） -----------------------------------
POLKIT_RULE = ROOT / "config" / "polkit" / "50-iot-monitor-restart.rules"


def test_polkitルールが存在する():
    """**これが無いと memory_guard は無力。** cron は tara0 権限で走るので
    systemctl restart が "Interactive authentication required" で失敗する
    （2026-10-24 に実測で確認した）。
    """
    assert POLKIT_RULE.exists()


def test_polkitルールが対象を絞っている():
    """全ユニットを許すと、このルール1枚で任意のサービスを操作できてしまう。"""
    text = POLKIT_RULE.read_text(encoding="utf-8")
    assert 'action.lookup("unit") !== "iot-monitor.service"' in text
    assert 'subject.user !== "tara0"' in text
    assert 'action.id !== "org.freedesktop.systemd1.manage-units"' in text


def test_polkitルールの括弧が閉じている():
    """polkit の JS エンジンは構文エラーのルールを黙って読み飛ばす。"""
    text = POLKIT_RULE.read_text(encoding="utf-8")
    for a, b in (("{", "}"), ("(", ")")):
        assert text.count(a) == text.count(b), f"{a}{b} の対応が崩れている"


def test_installがpolkitルールも入れる():
    text = INSTALL.read_text(encoding="utf-8")
    assert "50-iot-monitor-restart.rules" in text


def test_installが権限を動作で確認する():
    """設定値ではなく、実際に再起動できるかを確かめること。
    権限が無ければ guard は黙って無力になる。
    """
    text = INSTALL.read_text(encoding="utf-8")
    assert "sudo -u tara0 systemctl restart iot-monitor" in text
