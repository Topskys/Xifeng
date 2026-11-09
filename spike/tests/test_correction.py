"""模型纠错的守卫：这是"不把用户说的话改坏"的最后一道闸。

不加载真实模型（太慢、且输出不稳定），只测纯函数与降级路径。
真实模型的质量属于 Product eval，用 `quality_check.py --correct` 量。
"""

from __future__ import annotations

import pytest

from voiceime_spike import correction


# ---------------------------------------------------------------- 守卫：采纳

@pytest.mark.parametrize(
    ("src", "cand"),
    [
        ("这个会议机要发给我", "这个会议纪要发给我。"),
        ("用 voice i e 打字", "用 VoiceIME 打字"),
        ("请把 red me 文件发给张三", "请把 README 文件发给张三"),
        ("帮我把 voice i e 的 read me 就交就提交", "帮我把 VoiceIME 的 README 提交"),
        ("呃 今天 嗯 我们开个会", "呃 今天 我们 开个会"),
    ],
)
def test_reasonable_corrections_are_accepted(src: str, cand: str) -> None:
    assert correction.check(src, cand).ok


def test_case_and_space_fix_is_accepted() -> None:
    """read me → README 只改大小写/空格；这是最安全的改法。"""
    v = correction.check("请把 read me 发我", "请把README发我")
    assert v.ok and v.fixes == 1


# ---------------------------------------------------------------- 守卫：拒绝

@pytest.mark.parametrize(
    ("src", "cand", "reason"),
    [
        # 句首丢词：模型实测会把"帮我"吞掉
        ("帮我把 voice i e 的 read me 提交", "把VoiceIME的README提交", "drop-start"),
        # 只补句号：用户没说的标点不要硬加
        ("这个会议机要发给我", "这个会议机要发给我。", "punct-only"),
        # 句尾续写（三点 → 三点二十）
        ("今天的会推迟到下午三点", "今天的会推迟到下午三点二十。", "edge-add"),
        # 解释/标签/多行
        ("这个会议机要发给我", "输出：这个会议纪要发给我", "explain"),
        ("这个会议机要发给我", "这个会议纪要发给我\n\n解释：改了同音字", "explain"),
        # 原样返回
        ("这个会议机要发给我", "这个会议机要发给我", "same"),
        # 整句跑偏（从第一个字起就被替换）
        ("今天下午开会", "明天上午放假", "drop-start"),
        ("这个会议机要发给我", "", "empty"),
    ],
)
def test_dangerous_outputs_are_rejected(src: str, cand: str, reason: str) -> None:
    v = correction.check(src, cand)
    assert not v.ok
    assert v.reason == reason


def test_long_output_is_rejected() -> None:
    long = "这个会议纪要发给我" + "补充说明" * 30
    assert correction.check("这个会议机要发给我", long).reason == "too-long"


# ---------------------------------------------------------------- 输出清理

@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ('  "这个会议纪要发给我"  ', "这个会议纪要发给我"),
        ("“这个会议纪要发给我”", "这个会议纪要发给我"),
        ("输出：这个会议纪要发给我", "这个会议纪要发给我"),
        ("修正后: 这个会议纪要发给我", "这个会议纪要发给我"),
        ("这个会议纪要发给我", "这个会议纪要发给我"),
    ],
)
def test_clean_strips_quotes_and_labels(raw: str, want: str) -> None:
    assert correction._clean(raw) == want


# ---------------------------------------------------------------- 降级路径

def test_correct_without_model_returns_original() -> None:
    correction.reset()
    r = correction.correct("这个会议机要发给我")
    assert r.text == "这个会议机要发给我"
    assert not r.applied
    assert r.reason == "unavailable"


def test_load_missing_model_fails_softly(tmp_path) -> None:  # noqa: ANN001
    correction.reset()
    missing = tmp_path / "nope.gguf"
    assert correction.load(missing) is False
    assert "模型缺失" in correction.last_error()
    assert correction.warmup() == -1


def test_model_path_can_be_overridden(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    target = tmp_path / "m.gguf"
    monkeypatch.setenv("IME_LLM_MODEL", str(target))
    assert correction.model_path() == target


def test_llm_model_lives_in_data_dir_not_in_package(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """纠错模型默认放数据目录（与 SQLite 同处），不打进交付包。"""
    from voiceime_spike import paths

    data = tmp_path / "data"
    app = tmp_path / "app"
    monkeypatch.delenv("IME_LLM_MODEL", raising=False)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "app_dir", lambda: app)
    monkeypatch.setattr(paths, "is_frozen", lambda: True)

    expected = data / paths.LLM_SUBDIR
    assert paths.llm_model_path() == expected  # 缺失时也指向数据目录

    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"gguf")
    assert paths.llm_model_path() == expected


def test_llm_model_falls_back_to_exe_dir(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """旧布局（模型放在 exe 同级 models/llm）仍然可用。"""
    from voiceime_spike import paths

    data = tmp_path / "data"
    app = tmp_path / "app"
    monkeypatch.delenv("IME_LLM_MODEL", raising=False)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "app_dir", lambda: app)
    monkeypatch.setattr(paths, "is_frozen", lambda: True)

    legacy = app / paths.LLM_SUBDIR
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"gguf")
    assert paths.llm_model_path() == legacy


def test_unknown_failure_reason_is_not_fatal(monkeypatch) -> None:  # noqa: ANN001
    """推理抛异常必须回退原文，且原因只记类型（不记文本）。"""
    correction.reset()
    monkeypatch.setattr(correction, "_llm", object())

    def boom(*_a, **_kw):  # noqa: ANN002, ANN003
        raise RuntimeError("cuda exploded")

    monkeypatch.setattr(correction, "_complete", boom)
    r = correction.correct("这个会议机要发给我")
    assert r.text == "这个会议机要发给我"
    assert r.reason == "error:RuntimeError"
    assert "cuda" not in r.reason
