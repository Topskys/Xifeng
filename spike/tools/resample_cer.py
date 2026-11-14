"""G3 硬闸门：线性重采样 CER 对比。

壳（voice.rs Resampler）按设备原生格式开流后自己线性插值降到 16k，
替代了 Python 版的 WASAPI 系统重采样——这是新增的音频变换，必须证明
不劣化识别质量，否则静默伤 CER。

方法：fixtures/quality 的 16k 真人录音先高质量上采到 48k（模拟 WASAPI
原生流），再用与 Rust Resampler 逐行等价的算法（下混均值 + 线性插值，
step=rate/16000）降回 16k，复算 CER 与原路径对比。

    uv run python tools/resample_cer.py [--rate 48000]

判据：重采样路径 CER ≤ 原路径 CER + 3pp 且 ≤ 5%（合格线）才收；
劣化则改用 WASAPI 直接开 16k 流（plan.md D16-P1 风险登记）。
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures" / "quality"
OUT = ROOT.parent.parent / "env" / "tmp" / "cer_resampled"


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, path
        rate, frames = w.getframerate(), w.getnframes()
    pcm = np.frombuffer(path.read_bytes(), dtype=np.int16, offset=44)
    return rate, pcm[:frames].astype(np.float64)


def write_wav(path: Path, pcm16: np.ndarray, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.astype(np.int16).tobytes())


def upsample_high_quality(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """上采用 numpy 线性插值即可：上采不丢高频（0 阶保持误差远小于线性降采），
    且 fixture 本身 16k 带限，48k 上采后没有新信息。"""
    t = np.arange(0, len(pcm), src / dst)
    return np.interp(t, np.arange(len(pcm)), pcm)


def rust_resampler_equivalent(pcm: np.ndarray, rate: int) -> np.ndarray:
    """voice.rs Resampler::feed 的整段等价（无块边界 = prev 恒为前一输出样本）。

    下混（单声道跳过）→ step=rate/16000 线性插值 → round+clamp 回 i16。
    """
    step = rate / 16000.0
    n = len(pcm)
    t = 0.0
    out = []
    while t <= n:
        j = int(t)
        frac = t - j
        a = pcm[j - 1] if j > 0 else 0.0
        b = pcm[j] if j < n else pcm[n - 1]
        v = a + (b - a) * frac
        out.append(float(np.clip(round(v), -32767, 32767)))
        t += step
    return np.array(out, dtype=np.float64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=48000, help="模拟的原生采样率")
    args = ap.parse_args()

    for wav in sorted(FIXTURES.glob("*.wav")):
        src_rate, pcm = read_wav(wav)
        assert src_rate == 16000, f"fixture 应为 16k：{wav}"
        native = upsample_high_quality(pcm, 16000, args.rate)
        resampled = rust_resampler_equivalent(native, args.rate)
        # 尺寸对齐原路径（多一个样本裁掉，不影响 ASR）
        out = resampled[: len(pcm)]
        write_wav(OUT / wav.name, out)
        err = float(np.sqrt(np.mean((out - pcm) ** 2))) / 32767.0
        print(f"{wav.name}: 16k→{args.rate}→16k 帧数 {len(out)} vs 原 {len(pcm)}，RMS 相对误差 {err:.5f}")

    print(f"\n输出目录：{OUT}")
    print("下一步：uv run python quality_check.py --wav-dir", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
