"""管线串联：用 mock ASR 验证状态与 outcome 映射。

不测真实识别质量（那属于 Product eval，见 sdlc.md §6）。
"""

from __future__ import annotations

import numpy as np
import pytest

from xifeng import asr
from xifeng.audio import synth_speech_like
from xifeng.pipeline import Pipeline, PipelineConfig
from xifeng.trace import Outcome


@pytest.fixture(autouse=True)
def _mock_backend(monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("XIFENG_MOCK_TEXT", "呃 今天 我们 嗯 开个会。")
    asr.load(backend="mock")
    yield
    monkeypatch.delenv("XIFENG_MOCK_TEXT", raising=False)


def _pcm(seconds: float = 1.0) -> np.ndarray:
    return synth_speech_like(seconds)


def test_clean_text_reaches_postprocess() -> None:
    pipe = Pipeline(PipelineConfig(do_inject=False))
    r = pipe.process_pcm(_pcm())
    assert r.trace.outcome is Outcome.INJECTED
    assert r.text == "今天 我们 开个会。"
    assert r.trace.chars == len(r.text)
    assert r.trace.ms_asr >= 0


def test_hallucination_dropped_not_injected(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("XIFENG_MOCK_TEXT", "谢谢观看")
    pipe = Pipeline(PipelineConfig(do_inject=False))
    r = pipe.process_pcm(_pcm())
    assert r.trace.outcome is Outcome.DROPPED
    assert r.text is None


def test_empty_audio_dropped() -> None:
    pipe = Pipeline(PipelineConfig(do_inject=False))
    r = pipe.process_pcm(np.zeros(0, dtype=np.int16))
    assert r.trace.outcome is Outcome.DROPPED


def test_short_audio_returns_empty_text_dropped(monkeypatch) -> None:  # noqa: ANN001
    # mock 后端对 <0.3s 返回空串，等价于静音；这里去掉文本覆盖，走真实分支
    monkeypatch.delenv("XIFENG_MOCK_TEXT", raising=False)
    pipe = Pipeline(PipelineConfig(do_inject=False))
    r = pipe.process_pcm(np.zeros(1600, dtype=np.int16))
    assert r.trace.outcome is Outcome.DROPPED


def test_trace_written_when_sink_given(tmp_path) -> None:  # noqa: ANN001
    from xifeng.trace import TraceSink

    path = tmp_path / "trace.jsonl"
    pipe = Pipeline(PipelineConfig(do_inject=False), TraceSink(path))
    pipe.process_pcm(_pcm())
    pipe.process_pcm(_pcm())
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_vad_mode_still_single_inference() -> None:
    pipe = Pipeline(PipelineConfig(do_inject=False, use_vad=True))
    r = pipe.process_pcm(_pcm(1.5))
    assert r.trace.outcome is Outcome.INJECTED
    # VAD 只做一次 ASR，不按段多次推理
    assert r.trace.ms_asr >= 0


def test_vad_mode_silence_dropped() -> None:
    pipe = Pipeline(PipelineConfig(do_inject=False, use_vad=True))
    r = pipe.process_pcm(np.zeros(16000, dtype=np.int16))
    assert r.trace.outcome is Outcome.DROPPED


def test_ids_increase() -> None:
    pipe = Pipeline(PipelineConfig(do_inject=False))
    a = pipe.process_pcm(_pcm())
    b = pipe.process_pcm(_pcm())
    assert b.trace.id == a.trace.id + 1


# ---------------------------------------------------------------- 模型纠错（默认关）

def test_correction_off_by_default(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("XIFENG_MOCK_TEXT", "用 voice i e 打字")
    pipe = Pipeline(PipelineConfig(do_inject=False))
    r = pipe.process_pcm(_pcm())
    assert "voice i e" in r.text
    assert r.trace.fixes == 0
    assert r.trace.ms_correct == 0


def test_correction_applied_and_counted(monkeypatch) -> None:  # noqa: ANN001
    from xifeng import correction

    monkeypatch.setenv("XIFENG_MOCK_TEXT", "这个会议机要发给我")
    monkeypatch.setattr(
        correction,
        "correct",
        lambda text, **_kw: correction.CorrectionResult(
            text="这个会议纪要发给我", applied=True, reason="ok", fixes=1, ms=7
        ),
    )
    pipe = Pipeline(PipelineConfig(do_inject=False, model_correct=True))
    r = pipe.process_pcm(_pcm())
    assert r.text == "这个会议纪要发给我"
    assert r.trace.fixes == 1
    assert r.cleaned == "这个会议机要发给我"   # 中间级保留纠错前文本


def test_correction_rejection_keeps_original(monkeypatch) -> None:  # noqa: ANN001
    """守卫拒绝时必须原样注入 —— 宁可漏改，不能把用户说的话改坏（G32）。"""
    from xifeng import correction

    monkeypatch.setenv("XIFENG_MOCK_TEXT", "帮我把 voice i e 的 read me 提交")
    monkeypatch.setattr(
        correction,
        "correct",
        lambda text, **_kw: correction.CorrectionResult(text=text, applied=False, reason="drop-start"),
    )
    pipe = Pipeline(PipelineConfig(do_inject=False, model_correct=True))
    r = pipe.process_pcm(_pcm())
    assert r.text == "帮我把 voice i e 的 read me 提交"
    assert r.trace.fixes == 0


# ---------------------------------------------------------------- 静音裁剪

def test_silence_is_trimmed_before_asr() -> None:
    """按住不说话/说完还按着：静音必须被裁掉，否则模型会编话、ASR 也变慢。"""
    pcm = np.concatenate([
        np.zeros(16000, dtype=np.int16),
        _pcm(1.0),
        np.zeros(32000, dtype=np.int16),
    ])
    pipe = Pipeline(PipelineConfig(do_inject=False))
    r = pipe.process_pcm(pcm)
    assert r.trace.trimmed_ms >= 1500
    assert r.text == "今天 我们 开个会。"      # 内容不受影响


def test_trim_can_be_disabled() -> None:
    pcm = np.concatenate([np.zeros(16000, dtype=np.int16), _pcm(1.0)])
    pipe = Pipeline(PipelineConfig(do_inject=False, trim_silence=False))
    r = pipe.process_pcm(pcm)
    assert r.trace.trimmed_ms == 0


# ---------------------------------------------------------------- 注入策略


def test_injection_strategy_and_restore_delay_are_passed_through(monkeypatch) -> None:  # noqa: ANN001
    """实际用的策略要写进 Trace —— 否则"这条到底粘的还是打的"没法复盘。"""
    from xifeng import inject as inject_mod

    seen = {}

    def fake_inject(text, snap, cfg):  # noqa: ANN001, ANN202
        seen["strategy"] = cfg.strategy
        seen["restore_ms"] = cfg.clipboard_restore_ms
        return inject_mod.InjectResult(inject_mod.InjectOutcome.INJECTED, "clipboard")

    monkeypatch.setattr(inject_mod, "inject_ex", fake_inject)
    pipe = Pipeline(PipelineConfig(inject_strategy="clipboard", clipboard_restore_ms=123))
    r = pipe.process_pcm(_pcm())

    assert seen == {"strategy": "clipboard", "restore_ms": 123}
    assert r.trace.injection_strategy == "clipboard"
    assert r.trace.outcome is Outcome.INJECTED


def test_injection_failure_is_not_reported_as_success(monkeypatch) -> None:  # noqa: ANN001
    from xifeng import inject as inject_mod

    monkeypatch.setattr(
        inject_mod,
        "inject_ex",
        lambda text, snap, cfg: inject_mod.InjectResult(inject_mod.InjectOutcome.FAILED, "clipboard"),
    )
    pipe = Pipeline(PipelineConfig(inject_strategy="auto"))
    r = pipe.process_pcm(_pcm())

    assert r.trace.outcome is Outcome.FAILED
    assert r.trace.error == "Failed"
    assert r.trace.injection_strategy == "clipboard"


def test_pure_silence_is_not_trimmed() -> None:
    """全静音没有"有声区"可裁 —— 裁成空数组会让 ASR 连判断的机会都没有。"""
    pipe = Pipeline(PipelineConfig(do_inject=False))
    r = pipe.process_pcm(np.zeros(16000 * 3, dtype=np.int16))
    assert r.trace.trimmed_ms == 0
