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
    use_itn: bool = True


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

# ITN（逆文本归一化）默认开启：同时提供标点与数字改写（"三点"→"3点"）。
# 用户可在配置页或经 --no-itn 关闭。如实记账：中英混排可能明显退化
# （"voice i e … read me" → "voiceice imME的readmin"），见 docs/05-gotchas.md G12/G45。
DEFAULT_USE_ITN = True


#: 容器魔数 → 名字。只列**真实麦克风 PCM 几乎不可能以它开头**的那些：
#: 判定必须保守，误杀一段正常语音的代价比放过一个 WAV 文件大得多。
_CONTAINER_MAGIC: tuple[tuple[bytes, int, str], ...] = (
    (b"RIFF", 8, b"WAVE"),      # WAV：44 字节头会被当成采样
    (b"RIFF", 8, b"WEBP"),      # 少见，但同样是容器
    (b"FORM", 8, b"AIFF"),      # AIFF
    (b"FORM", 8, b"8SVX"),
    (b"fLaC", 0, b""),          # FLAC
    (b"OggS", 0, b""),          # Ogg / Opus
    (b"ID3", 0, b""),           # 带标签的 MP3
)


def _container_kind(pcm: bytes) -> str | None:
    """识别被误当 PCM 传进来的容器字节；不是容器返回 None。"""
    for magic, offset, extra in _CONTAINER_MAGIC:
        if pcm.startswith(magic) and (not extra or pcm[offset : offset + len(extra)] == extra):
            return magic.decode("ascii", "replace")
    return None


def _reject_container(pcm: bytes) -> None:
    """容器字节显式报错，不静默出垃圾（见 `transcribe` 文档）。"""
    kind = _container_kind(pcm)
    if kind is not None:
        raise AsrError(
            f"transcribe 收到的是音频容器（{kind}），不是裸 PCM。"
            "请用 xifeng.audio.read_wav() 取帧；MP3 需先解码成 16kHz 单声道 i16。"
        )


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

    # 换语言要重建 recognizer：先丢旧引用，让原生内存尽早回收
    # （sherpa 的 recognizer 持原生句柄，Python GC 不一定立刻释放）。
    old = _state.recognizer
    _state.recognizer = None
    if old is not None:
        del old

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
    """pcm 为小端 i16 单声道字节流。空文本是合法结果，不抛异常。

    容器字节（WAV/MP3）MUST NOT 直接进来：`np.frombuffer` 会把 RIFF 头
    当成采样、把压缩帧当成 PCM，**不报错**，只是吐出一段驴唇不对马嘴的
    文本——排查时最费时间的就是这种"看起来跑通了"的输入错误。
    读文件请用 `xifeng.audio.read_wav()`。
    """
    if _state.backend == "":
        load(backend="auto")

    _reject_container(pcm)

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
