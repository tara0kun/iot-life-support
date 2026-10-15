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
            # id=0 は未割当セッション用のセンチネル行で実在の人物ではない。
            # 名前が一般語なのでドキュメント中に普通に出てきて誤検知になる。
            rows = conn.execute("SELECT name FROM persons WHERE id != 0").fetchall()
        finally:
            conn.close()
        # 「祖母」「祖父」のような続柄は公開してよい。固有名だけを対象にする
        generic = {"祖母", "祖父", "母", "父", "家族", "孫", "不明",
                   "未確定", "未割当", "その他", "来客"}
        return [r[0] for r in rows if r[0] and r[0] not in generic and len(r[0]) >= 2]
    except Exception:
        return []


def _author_names() -> list[str]:
    """開発者本人の名前。

    `persons` は見守り対象の家族だけを持つので、開発者本人の名前は照合対象から
    漏れていた。実際に 2026-10-15 まで `docs/guide/rice-lid-sensor.md` に
    本名が平文で残り、public リポジトリに push されていた。
    git の author 名は必ずこのリポジトリにあるので、そこから取る。
    姓名の全体と、名の部分（3文字以上のときだけ）を見る。
    """
    import subprocess

    out: list[str] = []
    try:
        r = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, cwd=ROOT, timeout=5
        )
        full = r.stdout.strip()
    except Exception:
        return out
    if not full or len(full) < 2:
        return out
    out.append(full)
    given = full[2:]  # 日本語の姓は2文字が大半。名の部分も単独で書かれる
    if len(given) >= 2:
        out.append(given)
    return out


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


# 全ひらがなの名前は、日本語の一般語と同形になることがある。
# 例: 3文字の名前 + 助詞「に」が、そのまま副詞を構成してしまう。
# パターンだけでは原理的に区別できないので、衝突した語をここに溜める。
# **ここに入れてよいのは一般語だけ。名前そのものは絶対に書かないこと**
# （書いた瞬間にこのリポジトリが public な個人情報置き場になる）。
COMMON_WORD_COLLISIONS = {
    "はるかに",   # 副詞。src/web/app.py の「はるかに害が大きい」で衝突した
    "まさに",
    "さらに",
    "ともに",
    "すでに",
    "ゆえに",
}


def _collides_with_common_word(text: str, start: int, end: int) -> bool:
    """一致位置が一般語の一部なら True。"""
    for word in COMMON_WORD_COLLISIONS:
        lo = max(0, start - len(word))
        window = text[lo : end + len(word)]
        if word in window:
            # 一致範囲が word の内側に収まっているか
            for i in range(len(window) - len(word) + 1):
                if window[i : i + len(word)] == word:
                    w_start, w_end = lo + i, lo + i + len(word)
                    if w_start <= start and end <= w_end:
                        return True
    return False


def _is_ambiguous_name(name: str) -> bool:
    """日本語の文中で偶然一致しやすい名前か（2文字以下のひらがな）。"""
    return len(name) <= 2 and all("\u3041" <= ch <= "\u309f" for ch in name)


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
    for name in _family_names() + _author_names():
        if _is_ambiguous_name(name):
            # 2文字のひらがな名は日本語の文中で必ず偶然一致する。
            # 「〜な」+「お〜」の連結や、副詞の一部として現れるため、
            # 単語境界での判定が成立しない（具体例はここに書けない。
            # 書いた瞬間にこのチェック自身が引っかかるので）。
            # 名前として書かれている形（一覧・表・登録コマンド）に限って見る。
            # 前に区切りがあり、後ろが区切り/空白で終わる形だけを名前とみなす。
            # 行頭の「なお、」のような接続詞は前に区切りが無いので当たらない。
            # 後ろの「、」「。」は除く（名前は読点で終わらない）。
            # 前の区切りに改行を含めない。含めると行頭の接続詞
            # （「◯◯ 、」の形で文を始めるもの）が名前として当たってしまう。
            # [^\S\n] = 改行以外の空白。
            pat = (rf"(?:[・|/「『（(,、　]|[^\S\n]){re.escape(name)}"
                   rf"(?=\s*(?:$|[・|/」』）)　]|[^\S\n]|さん|ちゃん))")
        else:
            # **ひらがなの前後関係で絞ってはいけない。** 日本語では名前の直後に
            # 助詞（は・が・を・に・の…）が来るのが普通で、それ自体がひらがな。
            # (?![ぁ-ん]) を付けると「<名前>は」「<名前>が」が全て素通りする。
            # 3文字以上の名前なら偶然一致はまず起きないので素で探す。
            # 2文字以下のひらがな名だけは上の区切り付きパターンで扱う。
            pat = re.escape(name)
        for m in re.finditer(pat, text, re.MULTILINE):
            if _collides_with_common_word(text, m.start(), m.end()):
                continue
            hits.append(f"{label}: 家族の実名「{name}」")
            break
    for pat, why in PATTERNS:
        m = pat.search(text)
        if m:
            hits.append(f"{label}: {why} … {m.group(0)[:40]}")
    return hits


def main() -> int:
    args = sys.argv[1:]
    hits: list[str] = []

    # **フェイルクローズ。** 照合対象は .env（秘密）と persons テーブル（実名）から
    # 動的に読む設計なので、どちらも取れない環境（CI・別マシン・clone 直後）では
    # 何も検出できないまま exit 0 になり、チェックが通ったように見える。
    # 両方空なら「チェックできていない」と明示して落とす。
    if not _secrets() and not _family_names() and not _author_names():
        print("個人情報チェックの照合データが読めません（.env と persons の両方が空）。",
              file=sys.stderr)
        print("この環境ではチェックが成立しないため、安全側に倒して中止します。",
              file=sys.stderr)
        print("意図的に回避する場合は --no-verify を使ってください。", file=sys.stderr)
        return 1

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
