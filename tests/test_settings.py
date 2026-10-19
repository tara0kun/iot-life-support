"""設定値の解決順序。

2026-10-15 に実際に起きたバグの回帰テスト。
`scripts/anomaly_check.py` は `get_int("anomaly_inactivity_hours", 6)` と書いていたのに、
`SETTING_DEFAULTS` の "4" が勝って安否アラートが4時間で発火していた。
コメントには6時間が正しいと書いてあり、4時間だと約10日に1回誤報すると予測されていて、
実測でも15日で3件発火・うち2件を家族が誤検知として閉じた。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_解決順序はDB行が最優先(temp_db):
    from src.settings import get_setting, SETTING_DEFAULTS

    key = "anomaly_inactivity_hours"
    assert key in SETTING_DEFAULTS
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value, description) VALUES(?,?,?)",
        (key, "99", "test"),
    )
    conn.commit()
    conn.close()
    assert get_setting(key, "7") == "99", "DB に行があればそれが勝つ"


def test_DB行が無ければSETTING_DEFAULTSが勝つ(temp_db):
    from src.settings import get_setting, SETTING_DEFAULTS

    key = "anomaly_inactivity_hours"
    # 呼び出し側の default を渡しても DEFAULTS が優先される（これが今回のバグの本体）
    assert get_setting(key, "7") == SETTING_DEFAULTS[key][0]


def test_SETTING_DEFAULTSに無いキーだけ呼び出し側のdefaultが生きる(temp_db):
    from src.settings import get_setting, SETTING_DEFAULTS

    key = "この_キーは_存在しない_はず"
    assert key not in SETTING_DEFAULTS
    assert get_setting(key, "fallback") == "fallback"


def test_安否アラートのしきい値は6時間(temp_db):
    """4時間に戻ったら落ちる。誤報が約10日に1回に戻るため。"""
    from src.settings import get_int

    assert get_int("anomaly_inactivity_hours", 6) == 6


def test_呼び出し側のdefaultとSETTING_DEFAULTSが食い違っていない():
    """**このテストが今回のバグを自動で捕まえる。**

    SETTING_DEFAULTS にキーがある以上、呼び出し側が渡す default は必ず無視される。
    両者が食い違っていると、コードを読んだ人は呼び出し側の値が効くと誤解する。
    食い違いを見つけたら、どちらかに揃えること。
    """
    from src.settings import SETTING_DEFAULTS

    pat = re.compile(
        r'get_(?:int|bool|setting)\(\s*["\']([a-z_]+)["\']\s*,\s*([^)\s,]+)\s*\)'
    )
    mismatches = []
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            key, caller_default = m.group(1), m.group(2).strip()
            if key not in SETTING_DEFAULTS:
                continue
            declared = SETTING_DEFAULTS[key][0]
            literal = caller_default.strip("\"'")
            if caller_default in ("True", "False"):
                same = (declared == "1") == (caller_default == "True")
            elif caller_default.startswith("default="):
                continue  # キーワード引数はこの単純な正規表現では判定しない
            else:
                same = literal == declared
            if not same:
                mismatches.append(
                    f"{path.relative_to(ROOT)}: {key} 呼び出し側={caller_default} / DEFAULTS={declared}"
                )
    assert not mismatches, "呼び出し側の default が死んでいる:\n  " + "\n  ".join(mismatches)
