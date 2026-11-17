"""Trace 与分位数统计。字段口径见 docs/04-reference/metrics.md（M0 子集）。"""

from __future__ import annotations

import json

from xifeng.trace import Outcome, Trace, TraceSink, percentiles


def test_trace_has_stage_timings_and_no_text() -> None:
    t = Trace(id=1).start()
    t.ms_asr, t.ms_post, t.ms_inject, t.chars = 120, 1, 8, 6
    t.outcome = Outcome.INJECTED
    t.stop()

    d = t.to_dict()
    assert d["outcome"] == "Injected"
    assert {"ms_asr", "ms_post", "ms_inject", "e2e_ms", "chars"} <= set(d)
    # 隐私约束：Trace 里不允许出现识别文本
    assert not any(isinstance(v, str) and len(v) > 40 for v in d.values())
    assert t.e2e_ms >= 0


def test_trace_json_is_single_line_utf8() -> None:
    t = Trace(id=2).start()
    t.outcome = Outcome.DROPPED
    t.stop()
    line = t.to_json()
    assert "\n" not in line
    assert json.loads(line)["outcome"] == "Dropped"


def test_sink_appends_jsonl(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "logs" / "trace.jsonl"
    sink = TraceSink(path)
    for i in range(3):
        t = Trace(id=i).start()
        t.outcome = Outcome.INJECTED
        t.stop()
        sink.write(t)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert [json.loads(x)["id"] for x in lines] == [0, 1, 2]


def test_sink_without_path_is_noop() -> None:
    t = Trace().start()
    t.stop()
    TraceSink(None).write(t)


def test_percentiles_known_values() -> None:
    values = list(range(1, 101))
    p = percentiles(values)
    assert p["p50"] == 50
    assert p["p95"] == 95
    assert p["p99"] == 99


def test_percentiles_empty() -> None:
    assert percentiles([]) == {"p50": 0, "p95": 0, "p99": 0}
