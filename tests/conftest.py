"""テスト共通の足場。

**最優先の約束: 本番 DB に絶対に触らない。**
このリポジトリは祖母宅の Raspberry Pi 上で実稼働しており、tests/ は本番と
同じファイルシステム上で走る。うっかり本番 DB を掴むと実データが壊れる。
そのため以下の2段構えにしている。

1. `temp_db` fixture が `src.db.DB_PATH` を一時ファイルに差し替える
2. `_guard_production_db`（autouse）が、テスト中に DB_PATH が本番を指していたら
   その場で失敗させる
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PRODUCTION_DB = ROOT / "data" / "iot.db"


@pytest.fixture(autouse=True)
def _guard_production_db(request, monkeypatch):
    """本番 DB への接続そのものを遮断する安全弁。

    「テスト後に DB_PATH を見る」方式では、monkeypatch が先に巻き戻るので
    判定できない。接続の瞬間に止めるのが唯一確実な方法。
    `live` マーカーを付けたテストだけ通す。
    """
    if request.node.get_closest_marker("live"):
        return

    real_connect = sqlite3.connect
    target = str(PRODUCTION_DB.resolve())

    def guarded(database, *args, **kwargs):
        s = str(database)
        # file:/path/to/iot.db?mode=ro のような URI 形式も見る
        probe = s.split("?", 1)[0]
        if probe.startswith("file:"):
            probe = probe[5:]
        try:
            resolved = str(Path(probe).resolve())
        except (OSError, ValueError):
            resolved = probe
        if resolved == target or s.endswith("/data/iot.db"):
            raise RuntimeError(
                f"テストが本番 DB を開こうとした: {s}\n"
                "temp_db / seeded_db fixture を使うこと。"
                "本当に本番を読む必要があるなら @pytest.mark.live を付ける。"
            )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """スキーマだけ作った空の一時 DB に差し替える。

    `get_conn()` は呼び出し時にモジュール変数 DB_PATH を読むので、
    `from src.db import get_conn` で取り込んでいる側も差し替えが効く。
    """
    import src.db as db

    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield path


@pytest.fixture
def seeded_db(temp_db):
    """最低限の人物行を入れた DB。id=0 は未割当セッション用のセンチネル。"""
    conn = sqlite3.connect(temp_db)
    conn.executescript(
        """
        INSERT OR REPLACE INTO persons(id, name, role) VALUES
            (0, '未確定', 'family'),
            (1, 'テスト祖母', 'grandma'),
            (3, 'テスト家族', 'family');
        """
    )
    conn.commit()
    conn.close()
    return temp_db
