"""コミット前に個人情報が混入していないか検査する。

リポジトリは public だが、扱っているのは認知症で同意能力が限られる高齢者の
生活データ。2026-09-26 に家族UIのパスワードと本人の生活時刻が公開されていた
ことが発覚し、履歴の書き換えで対処した。同じことを繰り返さないための歯止め。

ルールを人の記憶に頼らないのが要点。検出したらコミットを止める。

使い方:
    python scripts/check_no_pii.py <file>...     # 指定ファイルを検査
    python scripts/check_no_pii.py --staged      # ステージ済みの差分を検査
    python scripts/check_no_pii.py --message <f> # コミットメッセージを検査
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# .env から読む秘密。ハードコードしない（この検査script自体が漏洩源にならないように）
SECRET_KEYS = [
    "FAMILY_PASSWORD", "TABLET_TOKEN", "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_CHANNEL_SECRET", "LINE_USER_ID", "TAPO_PASSWORD",
    "GRANDMA_WIFI_PASS", "SWITCHBOT_METER_MAC", "HEARTBEAT_URL",
]

# 家族の実名。person 名は DB から引く（ここに書くと本末転倒なので）
def _family_names() -> list[str]:
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{ROOT/'data'/'iot.db'}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT name FROM persons").fetchall()
        finally:
            conn.close()
        # 「祖母」「祖父」のような続柄は公開してよい。固有名だけを対象にする
        generic = {"祖母", "祖父", "母", "父", "家族", "孫", "不明"}
        return [r[0] for r in rows if r[0] and r[0] not in generic and len(r[0]) >= 2]
    except Exception:
        return []


def _secrets() -> list[tuple[str, str]]:
    out = []
    env = ROOT / ".env"
    if not env.exists():
        return out
    for line in env.read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in SECRET_KEYS and len(v) >= 4:
            out.append((k, v))
    return out


LIFE_WORDS = "入浴|お風呂|朝食|昼食|夕食|間食|夜食|トイレ|排泄|起床|就寝|服薬|お薬"
PATTERNS = [
    # 時刻と生活行動が同じ行に出る＝生活パターンの開示
    (re.compile(rf"\d{{1,2}}:\d{{2}}.*(?:{LIFE_WORDS})"), "時刻と生活行動が同じ行にある"),
    (re.compile(rf"(?:{LIFE_WORDS}).*\d{{1,2}}:\d{{2}}"), "生活行動と時刻が同じ行にある"),
    # 生活行動の回数
    (re.compile(rf"(?:{LIFE_WORDS})\s*[×x]?\s*\d+\s*回"), "生活行動の回数が書かれている"),
    # LINE の user id
    (re.compile(r"\bU[0-9a-f]{32}\b"), "LINE user ID らしき文字列"),
    # MAC アドレス
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"), "MACアドレス"),
]

SKIP_PREFIXES = ("data/", "venv/", ".git/", "logs/")


def scan(text: str, label: str) -> list[str]:
    hits = []
    for key, val in _secrets():
        if val in text:
            hits.append(f"{label}: .env の {key} の値")
    for name in _family_names():
        # 「はるかに」のような一般語との衝突を避け、単語境界に近い形だけ見る
        if re.search(rf"(?<![ぁ-ん]){re.escape(name)}(?![ぁ-ん])", text):
            hits.append(f"{label}: 家族の実名「{name}」")
    for pat, why in PATTERNS:
        m = pat.search(text)
        if m:
            hits.append(f"{label}: {why} … {m.group(0)[:40]}")
    return hits


def main() -> int:
    args = sys.argv[1:]
    hits: list[str] = []

    if args and args[0] == "--message":
        p = Path(args[1])
        hits += scan(p.read_text(errors="ignore"), "コミットメッセージ")
    elif args and args[0] == "--staged":
        names = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, cwd=ROOT).stdout.split()
        for n in names:
            if n.startswith(SKIP_PREFIXES):
                continue
            diff = subprocess.run(
                ["git", "diff", "--cached", "-U0", "--", n],
                capture_output=True, text=True, cwd=ROOT).stdout
            added = "\n".join(l[1:] for l in diff.splitlines()
                              if l.startswith("+") and not l.startswith("+++"))
            hits += scan(added, n)
    else:
        for n in args:
            hits += scan(Path(n).read_text(errors="ignore"), n)

    if hits:
        print("個人情報が含まれている可能性があります。コミットを中止しました。\n")
        for h in dict.fromkeys(hits):
            print(f"  - {h}")
        print("\nこのリポジトリは public です。扱っているのは実在する高齢者の生活データで、")
        print("本人は同意能力が限られます。技術的な説明は具体値なしでも成立します。")
        print("どうしても必要なら --no-verify で回避できますが、理由を考えてからにしてください。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
