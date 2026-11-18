"""质量评估口径的单元测试：CER 算错会直接误导"识别准不准"的结论。"""

from __future__ import annotations

import pytest

from quality_check import cer, diff_lines, levenshtein, normalize, verdict


def test_normalize_drops_punctuation_space_and_case() -> None:
    assert normalize("Hello，世界 ！") == "hello世界"
    assert normalize("今天下午3点开会。") == "今天下午3点开会"


def test_levenshtein_basic_cases() -> None:
    assert levenshtein("", "") == 0
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("abc", "abd") == 1          # 替换
    assert levenshtein("abc", "ab") == 1           # 漏
    assert levenshtein("abc", "abcd") == 1         # 多
    assert levenshtein("kitten", "sitting") == 3


def test_cer_zero_on_exact_match_ignoring_punctuation() -> None:
    assert cer("今天下午三点开会，把方案发给我。", "今天下午三点开会把方案发给我") == 0.0


def test_cer_counts_itn_rewrite_as_one_substitution() -> None:
    # ITN 把"三点"写成"3点"：G12 记录为 1 处替换，不是 0 也不是 2
    ref = "今天下午三点开会"
    hyp = "今天下午3点开会"
    assert cer(ref, hyp) == pytest.approx(1 / 8)


def test_cer_empty_hypothesis_is_total_loss() -> None:
    assert cer("你好世界", "") == 1.0
    assert cer("你好世界", "！！！") == 1.0      # 只有标点 → 归一化后为空


def test_cer_symmetric_length_normalization() -> None:
    # 分母用参考文本长度：多识别出的字也计为错误
    assert cer("你好", "你好你好") == 1.0


def test_verdict_thresholds() -> None:
    assert verdict(0.0) == "合格"
    assert verdict(0.05) == "合格"
    assert verdict(0.06) == "可接受"
    assert verdict(0.15) == "可接受"
    assert verdict(0.16) == "有问题"


def test_diff_lines_marks_substitution_and_missing() -> None:
    lines = "\n".join(diff_lines("今天开会", "今天开会吧"))
    assert "+ 吧" in lines or "吧" in lines

    lines2 = "\n".join(diff_lines("今天开会", "今天"))
    assert "漏" in lines2
