"""asr.transcribe 的输入契约：只吃裸 PCM，容器字节必须显式失败。

为什么单独守这条：把整个 WAV/MP3 文件当 PCM 喂进来**不会报错**，
只会吐出一段驴唇不对马嘴的文本——排查"本地比官方 demo 漏词"时，
这种输入错误能把人引到完全错误的方向（量化？VAD？后处理？）。
宁可拒绝，也不要静默出垃圾。
"""

from __future__ import annotations

import numpy as np
import pytest

from xifeng import asr, audio


def _fake_wav_bytes() -> bytes:
    """最小合法 RIFF/WAVE 头 + 一点假数据（模拟"整个文件读进来"）。"""
    import struct

    payload = b"\x00\x10" * 800
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(payload))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data"
        + struct.pack("<I", len(payload))
    )
    return header + payload


def test_wav_container_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asr._state, "backend", "mock")
    with pytest.raises(asr.AsrError, match="容器"):
        asr.transcribe(_fake_wav_bytes(), 16000)


def test_id3_mp3_container_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asr._state, "backend", "mock")
    with pytest.raises(asr.AsrError, match="容器"):
        asr.transcribe(b"ID3\x04\x00\x00\x00\x00\x00\x17" + b"\x00" * 400, 16000)


def test_error_message_points_at_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """报错要给出路：只说"不对"等于让人再猜一轮。"""
    monkeypatch.setattr(asr._state, "backend", "mock")
    with pytest.raises(asr.AsrError, match="read_wav"):
        asr.transcribe(_fake_wav_bytes(), 16000)


def test_real_pcm_is_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """语音级信号（首样本不为零）不该被误判成容器。"""
    monkeypatch.setattr(asr._state, "backend", "mock")
    pcm = audio.synth_speech_like(1.0).tobytes()
    out = asr.transcribe(pcm, 16000)
    assert out["text"]


def test_silence_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """全零 PCM（warmup 走的就是这条）不能被新校验挡住。"""
    monkeypatch.setattr(asr._state, "backend", "mock")
    out = asr.transcribe(b"\x00\x00" * 16000, 16000)
    assert out["audio_s"] == 1.0


def test_read_wav_roundtrips_official_style_file(tmp_path) -> None:
    """audio.read_wav 取到的帧长度 = 总帧数（不含 44 字节头）。"""
    import wave

    pcm = (np.sin(2 * np.pi * 440 * np.arange(16000) / 16000) * 3000).astype("<i2")
    path = tmp_path / "a.wav"
    audio.write_wav(path, pcm, 16000)
    data, rate = audio.read_wav(path)
    assert rate == 16000 and len(data) == 16000
    with wave.open(str(path), "rb") as f:
        assert f.getnframes() == 16000
        assert f.getcomptype() == "NONE"  # 未压缩 PCM（read_wav 只吃这种）
