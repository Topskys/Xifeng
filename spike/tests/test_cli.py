"""CLI 连线测试：子命令走通、参数传到位、`--text` 只做清理不加载模型。

不起麦克风、不加载模型、不注入 —— `--no-inject` + `--db` 指到临时库。
这些用例的意义是"命令行改坏了能立刻发现"，不是重复测业务逻辑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import run_spike
from voiceime_spike.store import Store


@pytest.fixture()
def db(tmp_path: Path) -> str:
    return str(tmp_path / "cli.db")


def test_text_mode_runs_cleanup_only(db: str, capsys) -> None:  # noqa: ANN001
    rc = run_spike.main(
        ["--text", "呃 帮我把 voice i e 的 read me 就交就提交", "--no-inject", "--db", db]
    )
    out = capsys.readouterr().out
    assert rc == 0
    cleaned = next(ln for ln in out.splitlines() if ln.startswith("[post] cleaned="))
    # 默认识别与纠错都不动英文术语；只清理语气词与多余空白
    assert "voice i e" in cleaned and "read me" in cleaned
    assert "[correct]" not in out


def test_text_mode_without_db_works(db: str, capsys) -> None:  # noqa: ANN001
    rc = run_spike.main(["--text", "用 voice i e 打字", "--no-inject", "--no-db"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "voice i e" in out


def test_no_inject_never_touches_keyboard(db: str, capsys) -> None:  # noqa: ANN001
    assert run_spike.main(["--text", "你好", "--no-inject", "--db", db]) == 0
    assert "[post] 判定：不注入" not in capsys.readouterr().out


def test_hallucination_reports_drop(db: str, capsys) -> None:  # noqa: ANN001
    rc = run_spike.main(["--text", "谢谢观看", "--no-inject", "--db", db])
    assert rc == 0
    assert "不注入" in capsys.readouterr().out


def test_stats_and_history_read_from_db(db: str, capsys) -> None:  # noqa: ANN001
    with Store(db) as s:
        s.add_history(_trace(), "之前的一条")

    assert run_spike.main(["--stats", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "总计 1 条" in out
    assert "P50=" in out

    assert run_spike.main(["--history", "5", "--db", db]) == 0
    assert "之前的一条" in capsys.readouterr().out


def test_stats_on_empty_db_is_ok(db: str, capsys) -> None:  # noqa: ANN001
    assert run_spike.main(["--stats", "--db", db]) == 0
    assert "总计 0 条" in capsys.readouterr().out


def test_readonly_subcommands_need_db(db: str) -> None:  # noqa: ANN001
    assert run_spike.main(["--stats", "--no-db"]) == 2


def _trace():  # noqa: ANN202
    from voiceime_spike.trace import Outcome, Trace

    return Trace(outcome=Outcome.INJECTED, chars=5, e2e_ms=300, ms_asr=250)
