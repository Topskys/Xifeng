"""postprocess 行为基准：这些用例是 Rust 侧 ime-post 的黄金用例（plan.md Step 2）。

规则来源：spec.md §4.3、docs/05-gotchas.md G5/G10。
"""

from __future__ import annotations

import pytest

from voiceime_spike import postprocess as pp


# ---------------------------------------------------------------- 幻觉（G10）


@pytest.mark.parametrize(
    "raw",
    [
        "谢谢观看",
        "谢谢大家观看。",
        "字幕由志愿者提供。",
        "请不吝点赞、订阅。",
        "OK",
    ],
)
def test_hallucination_returns_none(raw: str) -> None:
    assert pp.process(raw) is None


def test_hallucination_only_matches_whole_sentence() -> None:
    # 正文里出现"谢谢观看"不算幻觉
    assert pp.process("我在视频里听到谢谢观看这句话") is not None


# ---------------------------------------------------------------- 连续重复


def test_collapse_repeat_over_limit() -> None:
    raw = "好" * 20 + "的"
    assert pp.process(raw) == "好好的"


def test_keep_normal_repetition() -> None:
    assert pp.process("哈哈哈哈") == "哈哈哈哈"
    assert pp.process("哈哈") == "哈哈"


# ---------------------------------------------------------------- 语气词（G5）


def test_filler_at_sentence_start() -> None:
    assert pp.process("呃，今天开会。") == "今天开会。"


def test_filler_in_middle() -> None:
    assert pp.process("今天 嗯 开会。") == "今天 开会。"


def test_multiple_fillers_removed_in_one_pass() -> None:
    assert pp.process("呃 今天 我们 嗯 开个会，讨论一下 PyO3。") == "今天 我们 开个会，讨论一下 PyO3。"


def test_filler_inside_word_is_kept() -> None:
    # 「呃逆」是病名，删了就错
    assert pp.process("他最近呃逆得厉害。") == "他最近呃逆得厉害。"


def test_english_fillers_untouched() -> None:
    # 无 CJK 时整段跳过，er/um 是正常英文词
    assert pp.process("er, I think um, yes") == "er, I think um, yes"


def test_filler_only_sentence_drops() -> None:
    assert pp.process("嗯嗯") is None
    assert pp.process("呃。") is None


def test_filler_only_sentence_can_be_injected_when_configured() -> None:
    cfg = pp.PostConfig(standalone="inject")
    assert pp.process("嗯嗯", cfg) == "嗯嗯"


def test_sentence_final_particles_kept() -> None:
    # 「好啊」→「好」会改变语气，L3 默认不删
    assert pp.process("好啊") == "好啊"
    assert pp.process("走吧") == "走吧"


# ---------------------------------------------------------------- 归一化


def test_cjk_latin_spacing() -> None:
    assert pp.process("用PyO3接入") == "用 PyO3 接入"
    assert pp.process("讨论 一下PyO3的接入") == "讨论 一下 PyO3 的接入"


def test_duplicate_punctuation_collapsed() -> None:
    assert pp.process("真的吗？？？") == "真的吗？"


def test_blank_and_punctuation_only_dropped() -> None:
    assert pp.process("   ") is None
    assert pp.process("。。。") is None
    assert pp.process("，。？！") is None


def test_final_punct_strip_config() -> None:
    cfg = pp.PostConfig(final_punct="strip")
    assert pp.process("今天开会。", cfg) == "今天开会"


# ---------------------------------------------------------------- 幂等


@pytest.mark.parametrize(
    "raw",
    [
        "呃 今天 我们 嗯 开个会，讨论一下 PyO3 的接入。",
        "用PyO3接入",
        "哈哈哈哈",
        "他最近呃逆得厉害。",
    ],
)
def test_idempotent(raw: str) -> None:
    once = pp.process(raw)
    assert once is not None
    assert pp.process(once) == once


# ---------------------------------------------------------------- 统计


def test_stats_never_exposes_text() -> None:
    s = pp.stats("呃 今天开会", pp.process("呃 今天开会"))
    assert set(s) == {"chars_in", "chars_out", "dropped"}
    assert all(isinstance(v, int) for v in s.values())
