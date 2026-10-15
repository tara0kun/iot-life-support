"""個人情報チェック（scripts/check_no_pii.py）の検出性能を数字で固定する。

このリポジトリは public で、中身は認知症で同意能力が限られる高齢者の生活データ。
2026-09-26 に (a) 家族UIのパスワード (b) 食事写真 (c) 入浴・トイレの時刻が
public リポジトリに載っていたことが発覚した。このチェックは pre-commit と
commit-msg フックから呼ばれる最後の砦で、静かに壊れると次の漏洩に誰も気づけない。

実際に静かに壊れていた:

- 2026-10-15 まで、3文字以上の名前は直後に助詞（は・が・を・に・の…）が来ると
  必ず素通りしていた。`(?<![ぁ-ん])name(?![ぁ-ん])` を使っており、助詞そのものが
  ひらがななので先読みが必ず失敗する。日本語の文中で名前が裸で現れることはまず
  ないので、実名検出はほぼ死んでいた（実測45%）。commit 364889c で直した。
- 照合データ（.env と persons）が読めない環境では、何も検出しないまま exit 0 を
  返していた。「チェックが通った」と見えるのが最悪。commit 2d9499e で
  フェイルクローズにした。

**テストに実在の名前・パスワード・トークン・時刻を書かない。** 架空の値だけを使い、
照合先（モジュール変数 ROOT）を一時ディレクトリに差し替える。本番の .env と
data/iot.db は読まない。挙動を決めるのは文字数と文字種（ひらがなか否か）だけなので、
架空の語で十分に検証できる。
"""
from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_no_pii.py"

# ---------------------------------------------------------------- 架空データ
# 実名は書かない（書いた瞬間にこのチェック自身が引っかかる）。
# 「てすと」は3文字ひらがな = 曖昧名判定の境界（len<=2 のみ曖昧）を踏むために入れている。
FAKE_NAMES: tuple[str, ...] = ("架空太郎", "仮名花子", "試験子", "てすと", "テスト祖母")

# 2文字ひらがな名。日本語の文中に必ず偶然出てくる形の代表として、
# 名前ではありえない語をあえて選んでいる（実名との衝突を避けるため）。
AMBIGUOUS_NAMES: tuple[str, ...] = ("ので", "ずつ")

# persons の id=0 は未割当セッション用のセンチネル行。実在の人物ではない。
SENTINEL_NAME = "未確定"

FAKE_SECRETS: dict[str, str] = {
    "FAMILY_PASSWORD": "fake-family-pw-0000",
    "TABLET_TOKEN": "fake-tablet-token-0000",
    "LINE_CHANNEL_ACCESS_TOKEN": "fake-line-token-0000",
}

# 名前の直後に来る助詞。2026-10-15 まで、この11種類すべてで素通りしていた。
PARTICLES: tuple[str, ...] = (
    "は", "が", "を", "に", "の", "と", "も", "へ", "から", "より", "で",
)
# pytest は非ASCIIの parametrize id を \uXXXX に潰すので、読める id を明示する。
PARTICLE_IDS: tuple[str, ...] = (
    "wa", "ga", "wo", "ni", "no", "to", "mo", "he", "kara", "yori", "de",
)

# 検出率マトリクスの文型。助詞11種 + 名前が名前として書かれる6形。
NAME_TEMPLATES: tuple[str, ...] = tuple(
    f"{{name}}{p}今日の記録を確認した" for p in PARTICLES
) + (
    "担当は {name} さん",
    "| {name} | 3 |",
    "person add {name}",
    "・{name}",
    "（{name}）",
    "{name}、確認済み",
)

# ------------------------------------------- 生活パターンの検査データ
# **時刻リテラルと生活行動語を同じ行に書かない。** 書くとこのテストファイル自身が
# pre-commit の個人情報チェックに引っかかる（それが正しい動作）。部品を別の行に
# 置いて実行時に組み立てる。時刻はすべて架空で、本番 DB の値ではない。
SAMPLE_TIMES: tuple[str, ...] = ("07:30", "7:05", "21:15", "22:40")

# 実装の LIFE_WORDS の全要素。実装から import すると「語を消した」変更を
# 検出できなくなるので、期待値としてここに持つ。
LIFE_WORDS_EXPECTED: tuple[str, ...] = (
    "入浴", "お風呂", "朝食", "昼食", "夕食", "間食", "夜食",
    "トイレ", "排泄", "起床", "就寝", "服薬", "お薬",
)

# 時刻と生活行動が同じ行に出る形 = 生活パターンの開示
TIME_TEMPLATES: tuple[str, ...] = (
    "{time} に{word}を確認",
    "{word} {time} に開始",
    "{word}は {time}",
    "- {time} {word}",
)
# 生活行動の回数の形
COUNT_TEMPLATES: tuple[str, ...] = (
    "{word}×5回", "{word} 5 回", "{word}3回", "{word}x2回",
)

# 形だけ本物に似せた架空の識別子。実物の値は書かない。
FAKE_LINE_USER_ID = "U" + "0" * 32
FAKE_MAC = ":".join(["AA"] * 6)

# commit 364889c が「45% → 82%」と記録した数字。現状の実測は 88.2%。
# 下回ったら、名前の照合パターンが緩んだということ。
RATE_FLOOR = 0.82


# ------------------------------------------------------------------ 足場
def _load_check(root: Path) -> Any:
    """check_no_pii.py を独立したモジュールとして読み込み、照合先を root に向ける。

    scripts/ はパッケージではないので importlib で直接読む。テストごとに新しい
    モジュールを作るため、ROOT や _family_names を差し替えても他のテストに漏れない。
    """
    spec = importlib.util.spec_from_file_location("check_no_pii_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = root
    return mod


def _write_persons(root: Path, persons: Sequence[tuple[int, str]]) -> None:
    """偽リポジトリに persons テーブルだけの DB を置く。

    照合に使うのは id と name だけなので本物のスキーマは要らない。
    作成に URI 形式を使うのは、conftest の本番DBガードが「/data/iot.db で終わる
    パス」を全部止めるため（一時ディレクトリでも引っかかる）。検査対象の
    `_family_names()` 自身も URI 形式で開くので、読み出し経路は本番と同じ。
    """
    (root / "data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{root / 'data' / 'iot.db'}?mode=rwc", uri=True)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS persons(id INTEGER PRIMARY KEY, name TEXT, role TEXT)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO persons(id, name, role) VALUES(?, ?, 'family')",
            list(persons),
        )
        conn.commit()
    finally:
        conn.close()


def _write_env(root: Path, secrets: dict[str, str]) -> None:
    """偽の .env を置く。SECRET_KEYS に無いキーも混ぜて誤検知を見る。"""
    lines = [
        "# 架空の値。本番の .env は読まない",
        "NOT_A_SECRET=plain-value-0000",
    ] + [f"{k}={v}" for k, v in secrets.items()]
    (root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def make_checker(tmp_path: Path) -> Callable[..., Any]:
    """任意の persons / .env を持つ偽リポジトリを作り、そこを向いたモジュールを返す。"""

    def _make(
        *,
        persons: Sequence[tuple[int, str]] | None = None,
        secrets: dict[str, str] | None = None,
        name: str = "repo",
    ) -> Any:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        if persons is not None:
            _write_persons(root, persons)
        if secrets is not None:
            _write_env(root, secrets)
        return _load_check(root)

    return _make


@pytest.fixture
def checker(make_checker: Callable[..., Any]) -> Any:
    """本番と同じ構造（id=0 がセンチネル）の偽リポジトリを向いたモジュール。"""
    persons = [(0, SENTINEL_NAME)] + [
        (i, n) for i, n in enumerate(FAKE_NAMES, start=1)
    ]
    return make_checker(persons=persons, secrets=FAKE_SECRETS)


def _detection_rate(
    mod: Any, names: Iterable[str], templates: Iterable[str]
) -> tuple[float, list[str]]:
    """(名前 × 文型) の全組み合わせで検出率を測り、取りこぼしも返す。

    ここで測りたいのは名前の照合パターンだけなので、DB 読み出しは差し替えて
    名前リストを直接与える（モジュールはテストごとに新しいので漏れない）。
    """
    names = list(names)
    mod._family_names = lambda: list(names)
    missed: list[str] = []
    total = 0
    for name in names:
        for tpl in templates:
            text = tpl.format(name=name)
            total += 1
            if not any(f"「{name}」" in h for h in mod.scan(text, "t")):
                missed.append(text)
    return (total - len(missed)) / total, missed


# ------------------------------------------- 助詞（2026-10-15 の本体バグ）
@pytest.mark.parametrize("particle", PARTICLES, ids=PARTICLE_IDS)
def test_三文字以上の名前は直後に助詞が来ても検出される(checker: Any, particle: str) -> None:
    """2026-10-15 まで、この形は11種類の助詞すべてで素通りしていた。

    日本語では名前の直後に助詞が来るのが普通で、助詞自体がひらがななので
    `(?![ぁ-ん])` の先読みが必ず失敗する。実名検出がほぼ機能していなかった。
    名前の前後をひらがなで絞る実装に戻したら、ここが落ちる。
    """
    for name in FAKE_NAMES:
        text = f"{name}{particle}今日の記録を確認した"
        hits = checker.scan(text, "docs/x.md")
        assert any(f"「{name}」" in h for h in hits), f"助詞「{particle}」で素通り: {text}"


def test_三文字以上の名前の検出率は百パーセント(checker: Any) -> None:
    """commit 364889c が主張する「3文字名は検出率100%」を数字で固定する。

    1件でも取りこぼしたら、どの文型で落としたかを出す。
    """
    rate, missed = _detection_rate(checker, FAKE_NAMES, NAME_TEMPLATES)
    assert rate == 1.0, (
        f"3文字以上の名前の検出率が {rate:.1%} に落ちた。取りこぼし:\n  "
        + "\n  ".join(missed)
    )


def test_名前の検出率は全体で八割二分を下回らない(checker: Any) -> None:
    """2文字ひらがな名を混ぜた全体の検出率の床。

    2026-10-15 の修正前は 45%（この同じマトリクスでは 35.3%）だった。
    将来パターンを緩めて検出率が落ちたら、個別の文型を思いつかなくても
    この数字で気づける。
    """
    names = list(FAKE_NAMES) + [AMBIGUOUS_NAMES[0]]
    rate, missed = _detection_rate(checker, names, NAME_TEMPLATES)
    assert rate >= RATE_FLOOR, (
        f"検出率 {rate:.1%} が床 {RATE_FLOOR:.0%} を割った。取りこぼし:\n  "
        + "\n  ".join(missed)
    )


def test_助詞のひらがな先読みを戻すと検出率の床を割る(checker: Any) -> None:
    """**この床が機能していることの確認。**

    2026-10-15 まで使っていた式 `(?<![ぁ-ん])name(?![ぁ-ん])` を復元して同じ
    マトリクスを測ると 35.3% まで落ちる。上の床が「実装をなぞっただけで常に通る
    数字」ではない＝実際に回帰を捕まえられることを示す。
    """
    names = list(FAKE_NAMES) + [AMBIGUOUS_NAMES[0]]

    def old_scan(text: str, label: str) -> list[str]:
        return [
            f"{label}: 家族の実名「{n}」"
            for n in names
            if re.search(rf"(?<![ぁ-ん]){re.escape(n)}(?![ぁ-ん])", text, re.MULTILINE)
        ]

    old = types.SimpleNamespace(scan=old_scan)
    rate, _ = _detection_rate(old, names, NAME_TEMPLATES)
    assert rate < RATE_FLOOR, (
        f"修正前の式でも {rate:.1%} 出てしまった。床 {RATE_FLOOR:.0%} が低すぎて"
        "回帰を捕まえられない"
    )


# ------------------------------------------------------- 2文字ひらがな名
@pytest.mark.parametrize(
    "form",
    [
        "person add {name}",
        "| {name} | 3 |",
        "・{name}",
        "（{name}）",
        "担当は {name} さん",
        "・{name}ちゃん",
    ],
    ids=["space_eol", "pipe_table", "bullet", "paren", "space_san", "bullet_chan"],
)
def test_二文字ひらがな名は名前として書かれていれば検出される(
    make_checker: Any, form: str
) -> None:
    """2文字ひらがな名は、前後に区切りがある形（一覧・表・登録コマンド）だけ見る。

    文中での偶然一致を避けるための妥協で、名前として書かれている形は押さえる。
    区切りの一覧（・ | / 「 『 （ ( , 、 全角空白 空白、後ろは さん/ちゃん/行末も可）
    から文字が落ちたら、その形での漏洩を見逃す。
    """
    for name in AMBIGUOUS_NAMES:
        mod = make_checker(
            persons=[(0, SENTINEL_NAME), (1, name)],
            secrets=FAKE_SECRETS,
            name=f"repo_{name}",
        )
        text = form.format(name=name)
        assert mod.scan(text, "docs/x.md"), f"名前の形なのに素通り: {text}"


@pytest.mark.parametrize(
    "form",
    [
        "1件{name}処理する",
        "この{name}の変更で挙動が変わる",
        "{name}、確認した",
        "{name}は今日の記録を確認した",
    ],
    ids=["mid_word", "mid_phrase", "line_head_comma", "line_head_particle"],
)
def test_二文字ひらがな名は文中に埋もれていれば検出しない(
    make_checker: Any, form: str
) -> None:
    """**意図的な取りこぼし。** 誤検知ゼロと引き換えにしている。

    2文字ひらがなは日本語の文中に必ず偶然出てくる。ここを「検出するよう」直すと
    ほぼ全てのコミットが止まり、--no-verify が習慣になってチェック自体が死ぬ。
    3文字以上の名前に同じ妥協を広げたのが 2026-10-15 のバグなので、
    この妥協が2文字ひらがなに閉じていることを明示的に固定しておく。
    """
    for name in AMBIGUOUS_NAMES:
        mod = make_checker(
            persons=[(0, SENTINEL_NAME), (1, name)],
            secrets=FAKE_SECRETS,
            name=f"repo_{name}",
        )
        text = form.format(name=name)
        assert mod.scan(text, "docs/x.md") == [], f"文中の偶然一致で弾いた: {text}"


# ------------------------------------------------------------- 誤検知しない
def test_未確定センチネルは照合対象から外れている(checker: Any) -> None:
    """persons の id=0 は「未確定」という一般語のセンチネル行で、実在の人物ではない。

    照合対象に入っていると「未確定セッションを再集計した」のような普通の技術文書が
    個人情報として弾かれ、コミットできなくなる。2026-10-15 に id=0 を除外した
    （commit 364889c）。センチネルが照合対象に戻ったらこのテストが落ちる。
    """
    assert SENTINEL_NAME not in checker._family_names()
    assert checker.scan("未確定セッションを再集計した", "src/sessions.py") == []


def test_id0の行は名前が一般語でなくても照合しない(make_checker: Any) -> None:
    """除外は「名前が一般語だから」ではなく「id=0 はセンチネルだから」。

    一般語リストだけで弾いていると、センチネルの表示名を変えた瞬間に
    誤検知が復活する。id での除外が消えたら落ちる。
    """
    mod = make_checker(
        persons=[(0, "架空太郎"), (1, "仮名花子")], secrets=FAKE_SECRETS
    )
    assert mod._family_names() == ["仮名花子"]


@pytest.mark.parametrize(
    "word",
    ["祖母", "祖父", "母", "父", "家族", "孫", "不明", "未確定", "未割当", "その他", "来客"],
    ids=["sobo", "sofu", "haha", "chichi", "kazoku", "mago", "fumei",
         "mikakutei", "miwariate", "sonota", "raikyaku"],
)
def test_続柄と一般語は実名として扱わない(make_checker: Any, word: str) -> None:
    """ドキュメントは「祖母」「家族」で書く方針（固有名を出さない）。

    続柄が実名扱いになると、README も家族向け手順書も一切コミットできなくなる。
    """
    mod = make_checker(persons=[(0, SENTINEL_NAME), (5, word)], secrets=FAKE_SECRETS)
    assert word not in mod._family_names()
    assert mod.scan(f"{word}向けの手順書を更新した", "docs/guide/x.md") == []


def test_一文字の名前は照合しない(make_checker: Any) -> None:
    """1文字を照合対象にすると日本語の文に必ず当たって全コミットが止まる。

    `len(name) >= 2` の条件が消えたら落ちる。
    """
    mod = make_checker(persons=[(0, SENTINEL_NAME), (1, "椿")], secrets=FAKE_SECRETS)
    assert mod._family_names() == []


@pytest.mark.parametrize(
    "text",
    [
        "07:30 にバッチを実行する",
        "入浴判定のロジックを直した",
        "入浴セッションの ended_at を埋める",
        "5回リトライする",
        "健康チェックは5分間隔",
        "22:40:01 にエラーが出た",
    ],
    ids=["time_only", "life_word_only", "life_word_in_code",
         "count_without_life_word", "interval_minutes", "log_timestamp"],
)
def test_時刻だけ生活行動だけの行は落とさない(checker: Any, text: str) -> None:
    """生活パターンの判定は「時刻」と「生活行動」が同じ行に揃ったときだけ。

    片方だけで弾くと、入浴判定や食事検知のコードを直すコミットが毎回止まる。
    止まる回数が増えると --no-verify が習慣になり、本当の漏洩も止まらなくなる。
    """
    assert checker.scan(text, "src/logic.py") == []


def test_SECRET_KEYSに無いキーの値は照合しない(checker: Any) -> None:
    """.env の全行を照合すると、DB パスや閾値のような普通の設定値で誤検知する。"""
    assert checker.scan("plain-value-0000 を使う", "docs/x.md") == []


def test_四文字未満の秘密値は照合しない(make_checker: Any) -> None:
    """短い値はどんな文にも当たる。`len(v) >= 4` が消えると全コミットが止まる。"""
    mod = make_checker(
        persons=[(0, SENTINEL_NAME), (1, "架空太郎")],
        secrets={"FAMILY_PASSWORD": "ab"},
    )
    assert mod._secrets() == []
    assert mod.scan("abstract な説明を追記した", "docs/x.md") == []


# --------------------------------------------------------- 検出すべきもの
@pytest.mark.parametrize("key", sorted(FAKE_SECRETS))
def test_envの秘密値が本文に出たら検出される(checker: Any, key: str) -> None:
    """2026-09-26 に家族UIのパスワードが public リポジトリに載っていた。

    値はリポジトリに書かず .env から動的に読む設計なので、この経路が切れると
    同じ漏洩を素通りさせる。
    """
    hits = checker.scan(f"設定例: {FAKE_SECRETS[key]}", "README.md")
    assert any(key in h for h in hits), hits


def test_生活パターンは全ての生活行動語で検出される(checker: Any) -> None:
    """入浴とトイレの時刻は 2026-09-26 に実際に公開されていた3件のうちの1件。

    時刻→行動 / 行動→時刻 の両順と、回数の形（×・x・空白あり）を
    LIFE_WORDS の全13語で押さえる。語が1つ落ちると、その行動の生活パターンだけ
    静かに素通りするようになる。
    """
    missed: list[str] = []
    for word in LIFE_WORDS_EXPECTED:
        for sample_time in SAMPLE_TIMES:
            for tpl in TIME_TEMPLATES:
                text = tpl.format(time=sample_time, word=word)
                if not checker.scan(text, "PROGRESS.md"):
                    missed.append(text)
        for tpl in COUNT_TEMPLATES:
            text = tpl.format(word=word)
            if not checker.scan(text, "PROGRESS.md"):
                missed.append(text)
    assert not missed, "生活パターンを見逃した:\n  " + "\n  ".join(missed)


def test_LINEユーザIDとMACアドレスは検出される(checker: Any) -> None:
    """どちらも .env に実物があるが、値を書かずに「形」で押さえる。

    LINE user ID が漏れると、第三者が家族の通知先を特定できる。
    """
    assert checker.scan(FAKE_LINE_USER_ID, "docs/x.md")
    assert checker.scan(f"meter mac: {FAKE_MAC}", "docs/x.md")


# --------------------------------------------------- 入口（フックの経路）
def test_照合データが無い環境ではmainが1を返す(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """clone 直後・CI・別マシンでは .env も DB も無く、何も検出できない。

    commit 2d9499e まで、その状態で exit 0 を返していた。「チェックが通った」と
    見えるのが最悪で、照合不能と漏洩なしの区別がつかない。
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    clean = tmp_path / "clean.md"
    clean.write_text("入浴判定のロジックを直した\n", encoding="utf-8")

    mod = _load_check(bare)
    assert mod._secrets() == [] and mod._family_names() == []
    monkeypatch.setattr(sys, "argv", ["check_no_pii.py", str(clean)])
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "照合データが読めません" in err and "中止" in err, err


def test_照合データがあればクリーンなファイルは0を返す(
    checker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上のフェイルクローズの対照。

    1 が「照合不能」由来であって、クリーンなファイルを弾いているのではないことを
    示す。これが無いと、常に 1 を返す実装でもフェイルクローズのテストは通る。
    """
    clean = tmp_path / "clean.md"
    clean.write_text("入浴判定のロジックを直した\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_no_pii.py", str(clean)])
    assert checker.main() == 0


def test_秘密値を含むファイルはmainが1を返し値は出力しない(
    checker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ファイル指定の経路（手動実行）の端から端まで。

    加えて、出力に秘密値そのものを混ぜないこと。検出結果は端末やCIのログに残るので、
    値を出すと止めたはずの漏洩を自分で作る。
    """
    secret = FAKE_SECRETS["FAMILY_PASSWORD"]
    dirty = tmp_path / "dirty.md"
    dirty.write_text(f"家族UIのパスワードは {secret}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_no_pii.py", str(dirty)])

    assert checker.main() == 1
    out = capsys.readouterr().out
    assert "FAMILY_PASSWORD" in out
    assert secret not in out, "検出結果に秘密値そのものを出している"


def test_messageオプションでコミットメッセージを検査する(
    checker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """commit-msg フックの経路。

    「＜時刻＞の入浴を検知できていなかった件」のような書き方は実際にやりがちで、
    コミットメッセージは履歴から消しにくい（本文を直すには履歴の書き換えが要る）。
    """
    leak_line = TIME_TEMPLATES[1].format(word="入浴", time=SAMPLE_TIMES[2])
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"{leak_line} を修正\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_no_pii.py", "--message", str(msg)])
    assert checker.main() == 1


def _fake_git(names: Sequence[str], diffs: dict[str, str]) -> Callable[..., Any]:
    """git を呼ばずに --staged の経路を回すダミー。実リポジトリには触らない。"""

    def run(cmd: Sequence[str], **kwargs: Any) -> Any:
        if "--name-only" in cmd:
            return types.SimpleNamespace(stdout="\n".join(names) + "\n", returncode=0)
        return types.SimpleNamespace(stdout=diffs.get(cmd[-1], ""), returncode=0)

    return run


def test_staged差分では追加行だけを検査する(
    checker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pre-commit フックが実際に通る経路。

    追加行は弾き、削除行は弾かないのが両方とも要件。削除行まで検査すると、
    2026-09-26 に実際にやった「漏れた値をリポジトリから消すコミット」自体が
    永久に通らなくなる。
    """
    secret = FAKE_SECRETS["FAMILY_PASSWORD"]
    added = (
        "diff --git a/docs/x.md b/docs/x.md\n"
        "--- a/docs/x.md\n"
        "+++ b/docs/x.md\n"
        "@@ -0,0 +1 @@\n"
        f"+パスワードは {secret}\n"
    )
    removed = added.replace(f"+パスワードは {secret}", f"-パスワードは {secret}")
    monkeypatch.setattr(sys, "argv", ["check_no_pii.py", "--staged"])

    monkeypatch.setattr(
        checker, "subprocess",
        types.SimpleNamespace(run=_fake_git(["docs/x.md"], {"docs/x.md": added})),
    )
    assert checker.main() == 1, "追加行の秘密値を見逃した"

    monkeypatch.setattr(
        checker, "subprocess",
        types.SimpleNamespace(run=_fake_git(["docs/x.md"], {"docs/x.md": removed})),
    )
    assert checker.main() == 0, "削除行で弾いている。漏れた値を消すコミットが通らない"
