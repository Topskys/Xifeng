"""ASR 适配层：与 spec.md §4.1 的接口一一对应。

- backend="sherpa"：真实 SenseVoice（需要 sherpa-onnx + 模型文件）
- backend="mock"  ：固定文本，用于跑通管线与测试
- backend="auto"  ：能加载 sherpa 就用，否则退回 mock（并在 stderr 警告）

契约（M0）：
    load(model_dir, threads, backend) -> None
    transcribe(pcm_bytes, sample_rate) -> {"text", "lang", "rtf", "ms"}
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import envs, paths


class AsrError(RuntimeError):
    """推理失败。Rust 侧映射为 ImeError::Infer。"""


@dataclass
class _State:
    backend: str = ""
    recognizer: object | None = None
    model_dir: str = ""
    language: str = ""          # load() 时赋值，见 DEFAULT_LANGUAGE
    use_itn: bool = False


_state = _State()

MOCK_TEXT_ENV = "XIFENG_MOCK_TEXT"


def _mock_text(pcm: bytes, sample_rate: int) -> str:
    override = envs.get("MOCK_TEXT")
    if override is not None:
        return override
    seconds = len(pcm) / 2 / max(1, sample_rate)
    if seconds < 0.3:
        return ""
    return "呃 今天 我们 嗯 开个会，讨论一下 PyO3 的 接入方案。"


def _find_model(model_dir: Path) -> Path:
    for name in ("model.int8.onnx", "model.onnx"):
        p = model_dir / name
        if p.exists():
            return p
    raise AsrError(f"模型文件不存在: {model_dir}/model[.int8].onnx")


DEFAULT_THREADS = 8  # 8 核实测：2→404ms / 4→354ms / 8→329ms（4.49s 音频，P50）

# 语言 MUST 显式指定：auto 在本机把普通话判成粤语（"今天"→"听日"、"把方案发给我"→"落班返来"），
# 同一段音频 CER 57% → 0%。见 docs/05-gotchas.md G22。
DEFAULT_LANGUAGE = "zh"

# ITN（逆文本归一化）默认关闭：它把"一百五十"写成"150"（表面形式差异），
# 但混排英文会明显退化（"voice i e … read me" → "voiceice imME的readmin"，CER 4.5% → 27.3%）。
# 数字改写留给 postprocess 的规则，ASR 只负责给原文。见 docs/04-reference/metrics.md。
DEFAULT_USE_ITN = False


def load(
    model_dir: str | None = None,
    threads: int = DEFAULT_THREADS,
    backend: str = "auto",
    language: str = DEFAULT_LANGUAGE,
    use_itn: bool = DEFAULT_USE_ITN,
) -> None:
    """加载模型。重复调用幂等。"""
    model_dir = model_dir or str(paths.models_dir())

    if backend == "mock":
        _state.backend = "mock"
        _state.model_dir = model_dir
        return

    try:
        import sherpa_onnx  # noqa: F401
    except ImportError as exc:
        if backend == "sherpa":
            raise AsrError("未安装 sherpa-onnx：pip install sherpa-onnx") from exc
        print("[asr] sherpa-onnx 未安装，退回 mock 后端", flush=True)
        _state.backend = "mock"
        _state.model_dir = model_dir
        return

    d = Path(model_dir)
    if not d.exists():
        if backend == "sherpa":
            raise AsrError(f"模型目录不存在: {d}")
        print(f"[asr] 模型目录不存在({d})，退回 mock 后端", flush=True)
        _state.backend = "mock"
        _state.model_dir = model_dir
        return

    import sherpa_onnx

    model = _find_model(d)
    tokens = d / "tokens.txt"
    if not tokens.exists():
        raise AsrError(f"缺少 tokens.txt: {d}")

    _state.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model),
        tokens=str(tokens),
        num_threads=threads,
        language=language,
        use_itn=use_itn,
        debug=False,
        provider="cpu",
    )
    _state.backend = "sherpa"
    _state.model_dir = model_dir
    _state.language = language
    _state.use_itn = use_itn


def warmup() -> None:
    """用 1s 静音空跑一次，消除首包冷启动（docs/05-gotchas.md G8）。"""
    silence = b"\x00\x00" * 16000
    transcribe(silence, 16000)


def transcribe(pcm: bytes, sample_rate: int = 16000) -> dict:
    """pcm 为小端 i16 单声道字节流。空文本是合法结果，不抛异常。"""
    if _state.backend == "":
        load(backend="auto")

    t0 = time.perf_counter()

    if _state.backend == "mock":
        text = _mock_text(pcm, sample_rate)
        ms = int((time.perf_counter() - t0) * 1000)
        audio_s = len(pcm) / 2 / max(1, sample_rate)
        return {"text": text, "lang": "zh", "rtf": 0.0, "ms": ms, "audio_s": round(audio_s, 2)}

    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        return {"text": "", "lang": _state.language or "zh", "rtf": 0.0, "ms": 0, "audio_s": 0.0}

    try:
        stream = _state.recognizer.create_stream()  # type: ignore[union-attr]
        stream.accept_waveform(sample_rate, samples)
        _state.recognizer.decode_stream(stream)     # type: ignore[union-attr]
        text = stream.result.text or ""
    except Exception as exc:  # noqa: BLE001 - 统一映射成 AsrError
        raise AsrError(f"推理失败: {exc}") from exc

    ms = int((time.perf_counter() - t0) * 1000)
    audio_s = samples.size / sample_rate
    rtf = (ms / 1000) / audio_s if audio_s > 0 else 0.0
    return {
        "text": text,
        "lang": _state.language or "zh",
        "rtf": round(rtf, 3),
        "ms": ms,
        "audio_s": round(audio_s, 2),
    }


def backend() -> str:
    return _state.backend or "unloaded"


def config() -> dict:
    """当前 ASR 配置，供 Trace/日志记录（不含音频与文本）。"""
    return {
        "backend": backend(),
        "language": _state.language or DEFAULT_LANGUAGE,
        "use_itn": _state.use_itn,
        "model_dir": _state.model_dir,
    }
