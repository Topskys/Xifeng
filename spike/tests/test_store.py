"""SQLite 数据层：建库/迁移/历史/统计/设置/隐私开关。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xifeng.store import SCHEMA_VERSION, Store
from xifeng.trace import Outcome, Trace


def _trace(outcome: Outcome = Outcome.INJECTED, ts: str = "", **kw) -> Trace:  # noqa: ANN003
    t = Trace(outcome=outcome, ts=ts)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


@pytest.fixture()
def store(tmp_path: Path):  # noqa: ANN201
    s = Store(tmp_path / "xifeng.db")
    yield s
    s.close()


# ---------------------------------------------------------------- 建库与迁移

def test_creates_schema_at_current_version(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    with Store(db) as s:
        assert s.path == db
    assert db.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"history", "settings"} <= tables


def test_reopen_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    with Store(db) as s:
        s.add_history(_trace(chars=3), "你好啊")
    with Store(db) as s:
        assert s.count() == 1


def test_future_version_refuses_to_open(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with Store(db):
        pass
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        conn.commit()
    with pytest.raises(RuntimeError):
        Store(db)


# ---------------------------------------------------------------- 历史

def test_add_history_keeps_fields(store: Store) -> None:
    t = _trace(chars=5, ms_asr=321, ms_post=1, ms_inject=2, e2e_ms=400, speech_ms=2000)
    rid = store.add_history(t, "今天开会。", mode="ptt")
    assert rid > 0
    row = store.recent(1)[0]
    assert row["text"] == "今天开会。"
    assert row["mode"] == "ptt"
    assert row["outcome"] == "Injected"
    assert row["chars"] == 5
    assert row["ms_asr"] == 321
    assert row["e2e_ms"] == 400


def test_recent_is_newest_first_and_limited(store: Store) -> None:
    for i in range(5):
        store.add_history(_trace(chars=i), f"第{i}条")
    rows = store.recent(3)
    assert [r["text"] for r in rows] == ["第4条", "第3条", "第2条"]


def test_store_text_false_never_writes_text(tmp_path: Path) -> None:
    with Store(tmp_path / "p.db", store_text=False) as s:
        s.add_history(_trace(chars=4), "敏感内容")
        row = s.recent(1)[0]
    assert row["text"] is None
    assert row["chars"] == 4


def test_empty_text_is_not_stored(store: Store) -> None:
    store.add_history(_trace(), "")
    assert store.recent(1)[0]["text"] is None


def test_error_is_truncated(store: Store) -> None:
    store.add_history(_trace(Outcome.FAILED, error="x" * 500), None)
    assert len(store.recent(1)[0]["error"]) <= 200


# ---------------------------------------------------------------- 统计

def test_stats_aggregate(store: Store) -> None:
    store.add_history(_trace(e2e_ms=100, ms_asr=80, chars=10), "a")
    store.add_history(_trace(e2e_ms=200, ms_asr=120, chars=20), "b")
    store.add_history(_trace(Outcome.DROPPED, e2e_ms=300, ms_asr=160, chars=0), None)
    s = store.stats()
    assert s["total"] == 3
    assert s["injected"] == 2
    assert s["success_rate"] == pytest.approx(2 / 3)
    assert s["chars"] == 30
    assert s["e2e"]["p50"] == 200
    assert s["e2e"]["p95"] == 300
    assert s["asr"]["p95"] == 160
    assert s["by_outcome"] == {"Injected": 2, "Dropped": 1}


def test_stats_empty_db_does_not_divide_by_zero(store: Store) -> None:
    s = store.stats()
    assert s["total"] == 0
    assert s["success_rate"] == 0.0
    assert s["e2e"]["p95"] == 0


def test_stats_since_days_filters_old_rows(store: Store) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store.add_history(_trace(ts=old, e2e_ms=999), "old")
    store.add_history(_trace(e2e_ms=100), "new")
    assert store.stats()["total"] == 2
    assert store.stats(days=7)["total"] == 1


def test_prune_deletes_only_old_rows(store: Store) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store.add_history(_trace(ts=old), "old")
    store.add_history(_trace(), "new")
    assert store.prune(7) == 1
    assert store.count() == 1
    assert store.prune(0) == 0      # <=0 表示不清理


# ---------------------------------------------------------------- 设置

def test_settings_roundtrip(store: Store) -> None:
    assert store.get_setting("filler_level") is None
    assert store.get_setting("filler_level", "standard") == "standard"
    store.set_setting("filler_level", "deep")
    assert store.get_setting("filler_level") == "deep"
    store.set_setting("filler_level", "standard")   # 覆盖写
    assert store.get_setting("filler_level") == "standard"


def test_legacy_hotkey_value_normalized_to_one_side(store: Store) -> None:
    """G40：旧值 `ctrl`/`shift` 曾表示"左右均可"，读出来必须落到具体一侧。

    左右互斥后如果继续两侧都触发，用户设了哪一边都形同虚设；
    归一在 settings.load 做，老库不用重写。
    """
    from xifeng import settings

    store.set_setting("hotkey", "ctrl")
    assert settings.load(store).hotkey == "rctrl"
    store.set_setting("hotkey", "shift")
    assert settings.load(store).hotkey == "lshift"
    store.set_setting("hotkey", "lctrl")
    assert settings.load(store).hotkey == "lctrl"
    store.set_setting("hotkey", "bogus")
    assert settings.load(store).hotkey == settings.DEFAULTS.hotkey == "rctrl"


# ---------------------------------------------------------------- 默认路径

def test_default_path_honours_env(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    db = tmp_path / "custom.db"
    monkeypatch.setenv("IME_DB", str(db))
    with Store() as s:
        assert s.path == db
    assert db.exists()
