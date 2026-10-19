"""食事ラベルの定義がひとつであること（Issue #18）。

統合前は同等の集合が5ファイル9箇所にあり、中身が3通りに割れていた。
「夜食」を含むのは sessions.py だけ、「おやつ」を含まないものも2箇所。
家族UIが作る「外食(◯食)」形式はどれにも一致せず、コードのコメントが
「食事カウントに含まれる」と書いているのに実際には数えられていなかった。

統合時に実データで測った差分（全991セッション）:
  旧 375件 → 新 382件（夜食6件 + 外食(間食)1件）
  破棄マーク付き22件は引き続き数えない
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.sessions import MEAL_LABELS, is_meal_label, meal_label_sql

ROOT = Path(__file__).resolve().parent.parent


def test_定義はsessions_pyの1箇所だけ():
    """同等のラベル集合がどこかに再定義されたら落ちる。

    これが割れていると、同じ「食事回数」が画面ごとに違う数になる。
    """
    pattern = re.compile(r'\{\s*"朝食"[^}]*\}')
    offenders: list[str] = []
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        if path.name == "sessions.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line) and "起床" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, "食事ラベルが再定義されている:\n" + "\n".join(offenders)


@pytest.mark.parametrize("label", sorted(MEAL_LABELS))
def test_正準の各ラベルは食事として数える(label):
    assert is_meal_label(label) is True


def test_外食は中身の食事として数える():
    """家族UIは「外食(間食)」の形で書く。これを数えないと手動記録が消える。"""
    assert is_meal_label("外食(間食)") is True
    assert is_meal_label("外食（夕食）") is True
    assert is_meal_label("外食") is True


def test_破棄済みセッションは数えない():
    """クラスタリングで破棄されたものは食事ではない。"""
    assert is_meal_label("夕食 [モンスタークラスター/廃棄]") is False
    assert is_meal_label("夜食 [モンスタークラスター/廃棄]") is False


def test_食事でないラベルは数えない():
    for label in ("お風呂", "お薬", "起床", "就寝", "トイレ", "", None):
        assert is_meal_label(label) is False, label


def test_sqlとpythonの判定が一致する(temp_db):
    """SQL で絞った結果と Python で絞った結果がずれないこと。

    片方だけ直すと、画面の件数と通知の件数が食い違う。
    """
    import sqlite3

    samples = [
        "朝食", "昼食", "夕食", "間食", "夜食", "おやつ",
        "外食(間食)", "外食（夕食）",
        "お風呂", "お薬", "起床",
        "夕食 [モンスタークラスター/廃棄]",
        None,
    ]
    conn = sqlite3.connect(temp_db)
    try:
        for i, lb in enumerate(samples):
            conn.execute(
                "INSERT INTO meal_sessions(person_id, started_at, ended_at, "
                "event_count, label) VALUES(1, ?, ?, 1, ?)",
                (f"2026-06-15 1{i % 10}:00:00", f"2026-06-15 1{i % 10}:10:00", lb),
            )
        conn.commit()

        cond, params = meal_label_sql()
        sql_labels = {
            r[0] for r in conn.execute(
                f"SELECT label FROM meal_sessions WHERE {cond}", params
            )
        }
    finally:
        conn.close()

    py_labels = {lb for lb in samples if is_meal_label(lb)}
    assert sql_labels == py_labels, f"SQL={sorted(sql_labels)} Python={sorted(py_labels)}"


def test_meal_label_sqlは列名を差し替えられる():
    """JOIN したクエリで別名を使えること。"""
    cond, _ = meal_label_sql("m.label")
    assert "m.label" in cond
    assert "label IN" in cond or "m.label IN" in cond
