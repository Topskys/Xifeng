"""能量 VAD 的切分逻辑：M0 只验证"合并/丢弃/padding"，不评价识别质量。"""

from __future__ import annotations

import numpy as np

from xifeng import vad
from xifeng.audio import synth_speech_like

SR = 16000


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.int16)


def test_silence_has_no_segment() -> None:
    assert vad.segment(_silence(1.0)) == []


def test_empty_input() -> None:
    assert vad.segment(np.zeros(0, dtype=np.int16)) == []


def test_single_speech_burst_detected() -> None:
    pcm = np.concatenate([_silence(0.5), synth_speech_like(1.5), _silence(0.5)])
    segs = vad.segment(pcm)
    assert len(segs) == 1
    start, end = segs[0]
    # 允许 padding 外扩，但不能漏掉语音主体（0.5s ~ 2.0s）
    assert start <= int(0.5 * SR)
    assert end >= int(1.9 * SR)


def test_short_burst_dropped() -> None:
    # 100ms 的咔哒声应被 min_speech_ms=250 丢弃
    pcm = np.concatenate([_silence(0.3), synth_speech_like(0.1), _silence(0.3)])
    assert vad.segment(pcm) == []


def test_head_pad_extends_first_segment() -> None:
    """G39：首段起点额外留白，否则 Silero 起音延迟会切掉第一个音节。"""
    pcm = np.concatenate([_silence(0.5), synth_speech_like(1.5), _silence(0.5)])
    head = int(SR * vad.VadConfig().head_pad_ms / 1000)
    with_head = vad.segment(pcm, vad.VadConfig())[0][0]
    without = vad.segment(pcm, vad.VadConfig(head_pad_ms=0, tail_pad_ms=0))[0][0]
    # 段首应比「不留白」提前 head（受 0 截断）
    assert without - with_head == min(head, without)


def test_internal_boundary_pad_stays_small() -> None:
    """内部边界只按 pad_ms 扩：留太多会重叠出重复音频，诱发复读。"""
    pcm = np.concatenate(
        [
            _silence(0.3),
            synth_speech_like(0.8),
            _silence(0.8),          # > merge_gap_ms(200)
            synth_speech_like(0.8),
            _silence(0.3),
        ]
    )
    segs = vad.segment(pcm)
    assert len(segs) == 2
    assert segs[1][0] >= segs[0][1], f"内部边界不该重叠: {segs}"


# ---------------------------------------------------------------- 首尾静音裁剪

def test_trim_removes_lead_and_tail_silence() -> None:
    pcm = np.concatenate([_silence(1.0), synth_speech_like(1.5), _silence(2.0)])
    out, saved = vad.trim(pcm, sample_rate=SR)
    assert 0 < len(out) < len(pcm)
    assert saved >= 1500            # 前后 3s 静音，至少省 1.5s
    assert len(out) >= int(1.5 * SR)  # 语音主体不能少


def test_trim_keeps_padding_around_speech() -> None:
    pcm = np.concatenate([_silence(2.0), synth_speech_like(1.0), _silence(2.0)])
    out, _ = vad.trim(pcm, sample_rate=SR)
    # 语音从 2.0s 开始；留 200ms 前置 → 裁剪起点不晚于 1.8s 对应的位置
    assert len(out) >= int(1.0 * SR)


def test_trim_pure_silence_is_noop() -> None:
    """全静音不能裁成空数组 —— 让 ASR 决定（它返回空串 → Dropped）。"""
    pcm = _silence(3.0)
    out, saved = vad.trim(pcm, sample_rate=SR)
    assert saved == 0
    assert out.size == pcm.size


def test_trim_short_blip_is_noop() -> None:
    pcm = np.concatenate([_silence(1.0), synth_speech_like(0.1), _silence(1.0)])
    _, saved = vad.trim(pcm, sample_rate=SR)
    assert saved == 0


def test_trim_continuous_speech_saves_little() -> None:
    pcm = synth_speech_like(2.0)
    _, saved = vad.trim(pcm, sample_rate=SR)
    assert saved <= 300


def test_trim_empty_input() -> None:
    out, saved = vad.trim(np.zeros(0, dtype=np.int16), sample_rate=SR)
    assert out.size == 0 and saved == 0


def test_trim_handles_quiet_recording() -> None:
    """整体音量很低时也要能裁：阈值按峰值/噪声自适应，不是绝对音量。"""
    pcm = np.concatenate([_silence(1.5), (synth_speech_like(1.0) * 0.05).astype(np.int16), _silence(1.5)])
    out, saved = vad.trim(pcm, sample_rate=SR)
    assert saved >= 1000
    assert len(out) >= int(1.0 * SR)


def test_two_bursts_with_long_gap_kept_separate() -> None:
    pcm = np.concatenate(
        [
            _silence(0.3),
            synth_speech_like(0.8),
            _silence(0.8),          # > merge_gap_ms(200)
            synth_speech_like(0.8),
            _silence(0.3),
        ]
    )
    assert len(vad.segment(pcm)) == 2


def test_close_bursts_merged() -> None:
    pcm = np.concatenate(
        [
            _silence(0.3),
            synth_speech_like(0.6),
            _silence(0.1),          # < merge_gap_ms
            synth_speech_like(0.6),
            _silence(0.3),
        ]
    )
    assert len(vad.segment(pcm)) == 1


def test_rebuild_drops_silence() -> None:
    speech = synth_speech_like(1.0)
    pcm = np.concatenate([_silence(1.0), speech, _silence(1.0)])
    segs = vad.segment(pcm)
    rebuilt = vad.rebuild(pcm, segs)
    assert rebuilt.dtype == np.int16
    # 至少删掉一半静音，且保留语音长度
    assert len(rebuilt) < len(pcm)
    # 允许帧量化误差（1 帧 = 30ms）
    assert len(rebuilt) >= len(speech) - 480


def test_rebuild_without_segments_is_empty() -> None:
    assert len(vad.rebuild(_silence(1.0), [])) == 0


# ---------------------------------------------------------------- Silero 概率切分（M1）

from xifeng.vad import VadConfig, probs_to_segments  # noqa: E402

WINDOW = 512


def _probs(rates: list[tuple[float, int]]) -> list[float]:
    """构造概率流：[(概率, 窗数), ...] 展开。"""
    out: list[float] = []
    for p, n in rates:
        out.extend([p] * n)
    return out


def test_probs_between_thresholds_neither_starts_nor_ends() -> None:
    # 0.4 低于 start(0.5) 但高于 end(0.35)：不进入语音
    cfg = VadConfig()
    raw = probs_to_segments(_probs([(0.4, 20)]), cfg, WINDOW, 20 * WINDOW)
    assert raw == []


def test_probs_start_after_confirm_frames() -> None:
    cfg = VadConfig()
    # 2 窗高概率不够 start_frames=3，第 3 窗才确认；seg_start 回退到首窗
    probs = [0.0] * 10 + [0.9, 0.9] + [0.9] * 20
    raw = probs_to_segments(probs, cfg, WINDOW, len(probs) * WINDOW)
    assert len(raw) == 1
    assert raw[0][0] == 10 * WINDOW          # 回退到第一个高概率窗
    assert raw[0][1] == len(probs) * WINDOW  # 说完还没退出 → 到末尾


def test_probs_end_after_silence_frames() -> None:
    cfg = VadConfig()
    # 连续 8 窗 < end_prob 才退出；段尾不含 hangover
    probs = [0.9] * 20 + [0.1] * 8 + [0.9] * 20
    raw = probs_to_segments(probs, cfg, WINDOW, len(probs) * WINDOW)
    assert len(raw) == 2
    assert raw[0][1] == 20 * WINDOW          # 最后一个 >= end_prob 的窗
    assert raw[1][0] == 28 * WINDOW          # 第二段从重新确认处开始


def test_probs_short_burst_dropped_by_finalize() -> None:
    cfg = VadConfig()
    # 100ms 的毛刺（约 6 窗）→ 段长 < min_speech_ms=250 被丢弃
    probs = [0.0] * 30 + [0.9] * 6 + [0.0] * 30
    segs = probs_to_segments(probs, cfg, WINDOW, len(probs) * WINDOW)
    assert vad._finalize(segs, len(probs) * WINDOW, cfg) == []


def test_probs_close_bursts_merged() -> None:
    # 注意：滞回本身会吸收 < end_frames(8窗) 的短停顿；两个 raw 段之间
    # 至少隔 8(退出确认)+2(进入确认) 窗 ≈ 320ms，所以合并要靠调大 merge_gap
    cfg = VadConfig(merge_gap_ms=350)
    probs = [0.9] * 30 + [0.1] * 10 + [0.9] * 30   # 间隔 ~320ms
    segs = vad._finalize(
        probs_to_segments(probs, cfg, WINDOW, len(probs) * WINDOW),
        len(probs) * WINDOW, cfg,
    )
    assert len(segs) == 1


def test_probs_short_pause_absorbed_by_hysteresis() -> None:
    # 停顿 6 窗（~190ms）< end_frames=8：根本不退出，就是一段
    cfg = VadConfig()
    probs = [0.9] * 30 + [0.1] * 6 + [0.9] * 30
    segs = vad._finalize(
        probs_to_segments(probs, cfg, WINDOW, len(probs) * WINDOW),
        len(probs) * WINDOW, cfg,
    )
    assert len(segs) == 1


def test_probs_isolated_bursts_stay_separate() -> None:
    cfg = VadConfig()
    probs = [0.9] * 40 + [0.1] * 20 + [0.9] * 40   # 间隔 ~640ms > merge_gap
    segs = vad._finalize(
        probs_to_segments(probs, cfg, WINDOW, len(probs) * WINDOW),
        len(probs) * WINDOW, cfg,
    )
    assert len(segs) == 2
