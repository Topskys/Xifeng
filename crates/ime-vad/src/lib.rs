//! 语音活动检测：能量法切分、Silero 概率双阈值滞回、首尾静音裁剪。
//!
//! 与 `spike/voiceime_spike/vad.py` 逐字节等价，由
//! `spike/tests/test_vad_parity.py` 校验。
//!
//! 只搬纯计算部分：Silero 模型前向仍留在 Python（sherpa-onnx），
//! 这里接的是它吐出的概率流。

/// VAD 参数，字段与 Python `VadConfig` 一一对应。
#[derive(Debug, Clone)]
pub struct VadConfig {
    pub sample_rate: i32,
    pub frame_ms: i32,
    pub start_ratio: f64,
    pub end_ratio: f64,
    pub start_prob: f64,
    pub end_prob: f64,
    pub start_frames: usize,
    pub end_frames: usize,
    pub min_speech_ms: i32,
    pub merge_gap_ms: i32,
    pub pad_ms: i32,
}

impl Default for VadConfig {
    fn default() -> Self {
        Self {
            sample_rate: 16000,
            frame_ms: 30,
            start_ratio: 0.5,
            end_ratio: 0.35,
            start_prob: 0.5,
            end_prob: 0.35,
            start_frames: 3,
            end_frames: 8,
            min_speech_ms: 250,
            merge_gap_ms: 200,
            pad_ms: 60,
        }
    }
}

/// 首尾静音裁剪参数，对应 Python `TrimConfig`。
#[derive(Debug, Clone)]
pub struct TrimConfig {
    pub frame_ms: i32,
    pub peak_ratio: f64,
    pub noise_ratio: f64,
    pub abs_floor: f64,
    pub lead_ms: i32,
    pub tail_ms: i32,
    pub min_speech_ms: i32,
    pub min_saved_ratio: f64,
}

impl Default for TrimConfig {
    fn default() -> Self {
        Self {
            frame_ms: 30,
            peak_ratio: 0.05,
            noise_ratio: 3.0,
            abs_floor: 200.0,
            lead_ms: 200,
            tail_ms: 300,
            min_speech_ms: 250,
            min_saved_ratio: 0.10,
        }
    }
}

/// 计算一帧的 RMS（先转 f64 再平方，与 numpy 的 float64 累加一致）。
fn rms(frame: &[i16]) -> f64 {
    if frame.is_empty() {
        return 0.0;
    }
    let mut acc = 0.0f64;
    for &v in frame {
        let x = v as f64;
        acc += x * x;
    }
    (acc / frame.len() as f64).sqrt()
}

/// 与 numpy.percentile(x, 20) 的默认线性插值等价。
fn percentile20(values: &[f64]) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut v = values.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    if v.len() == 1 {
        return v[0];
    }
    // numpy 默认 method='linear'：位置 = q/100 * (n-1)
    let pos = 0.20 * (v.len() - 1) as f64;
    let lo = pos.floor() as usize;
    let hi = pos.ceil() as usize;
    if lo == hi {
        v[lo]
    } else {
        let frac = pos - lo as f64;
        v[lo] * (1.0 - frac) + v[hi] * frac
    }
}

/// 切分后处理（能量法与 Silero 共用）：丢弃过短 → 合并相近 → 加 padding。
fn finalize(raw: &[(usize, usize)], pcm_len: usize, cfg: &VadConfig) -> Vec<(usize, usize)> {
    let min_len = (cfg.sample_rate as i64 * cfg.min_speech_ms as i64 / 1000) as usize;
    let kept: Vec<(usize, usize)> = raw
        .iter()
        .copied()
        .filter(|&(s, e)| e.saturating_sub(s) >= min_len)
        .collect();

    let gap = (cfg.sample_rate as i64 * cfg.merge_gap_ms as i64 / 1000) as usize;
    let mut merged: Vec<(usize, usize)> = Vec::with_capacity(kept.len());
    for (s, e) in kept {
        match merged.last_mut() {
            Some(last) if s.saturating_sub(last.1) < gap => last.1 = e,
            _ => merged.push((s, e)),
        }
    }

    let pad = (cfg.sample_rate as i64 * cfg.pad_ms as i64 / 1000) as usize;
    merged
        .into_iter()
        .map(|(s, e)| (s.saturating_sub(pad), (e + pad).min(pcm_len)))
        .collect()
}

/// 能量法切分，返回语音段 `[(start_sample, end_sample), ...]`。
pub fn segment(pcm: &[i16], cfg: &VadConfig) -> Vec<(usize, usize)> {
    let frame_len = (cfg.sample_rate as i64 * cfg.frame_ms as i64 / 1000).max(1) as usize;
    if pcm.len() < frame_len {
        return Vec::new();
    }

    // 不重叠帧（hop == frame_len），与 Python 的 _frames(pcm, fl, fl) 一致
    let frames: Vec<&[i16]> = pcm.chunks_exact(frame_len).collect();
    if frames.is_empty() {
        return Vec::new();
    }

    let energies: Vec<f64> = frames.iter().map(|f| rms(f)).collect();
    let peak = energies.iter().copied().fold(0.0f64, f64::max);
    if peak <= 1e-6 {
        return Vec::new();
    }

    let start_th = peak * cfg.start_ratio;
    let end_th = peak * cfg.end_ratio;

    let mut raw: Vec<(usize, usize)> = Vec::new();
    let mut speaking = false;
    let mut seg_start = 0usize;
    let mut above = 0usize;
    let mut below = 0usize;
    let mut last_active = 0usize;

    for (i, &e) in energies.iter().enumerate() {
        if !speaking {
            above = if e >= start_th { above + 1 } else { 0 };
            if above >= cfg.start_frames {
                speaking = true;
                seg_start = i.saturating_sub(cfg.start_frames - 1) * frame_len;
                below = 0;
                last_active = i;
            }
        } else if e >= end_th {
            last_active = i;
            below = 0;
        } else {
            below += 1;
            if below >= cfg.end_frames {
                speaking = false;
                let seg_end = ((last_active + 1) * frame_len).min(pcm.len());
                raw.push((seg_start, seg_end));
                above = 0;
            }
        }
    }

    if speaking {
        raw.push((
            seg_start,
            ((last_active + 1) * frame_len).min(pcm.len()),
        ));
    }

    finalize(&raw, pcm.len(), cfg)
}

/// 概率流 → 原始语音段（双阈值滞回）。纯函数，不依赖模型。
pub fn probs_to_segments(
    probs: &[f64],
    cfg: &VadConfig,
    window: usize,
    total_samples: Option<usize>,
) -> Vec<(usize, usize)> {
    let total = total_samples.unwrap_or(probs.len() * window);
    let mut raw: Vec<(usize, usize)> = Vec::new();
    let mut speaking = false;
    let mut seg_start = 0usize;
    let mut above = 0usize;
    let mut below = 0usize;
    let mut last_active = 0usize;

    for (i, &p) in probs.iter().enumerate() {
        if !speaking {
            above = if p >= cfg.start_prob { above + 1 } else { 0 };
            if above >= cfg.start_frames {
                speaking = true;
                seg_start = i.saturating_sub(cfg.start_frames - 1) * window;
                below = 0;
                last_active = i;
            }
        } else if p >= cfg.end_prob {
            last_active = i;
            below = 0;
        } else {
            below += 1;
            if below >= cfg.end_frames {
                speaking = false;
                raw.push((seg_start, ((last_active + 1) * window).min(total)));
                above = 0;
            }
        }
    }

    if speaking {
        raw.push((seg_start, ((last_active + 1) * window).min(total)));
    }
    raw
}

/// 概率流切分 + 后处理（供 Python 直接调用，省得在外面再拼一次 finalize）。
pub fn segment_from_probs(
    probs: &[f64],
    cfg: &VadConfig,
    window: usize,
    total_samples: usize,
) -> Vec<(usize, usize)> {
    let raw = probs_to_segments(probs, cfg, window, Some(total_samples));
    finalize(&raw, total_samples, cfg)
}

/// 把多个语音段拼回单条 PCM。只做一次 ASR 是关键：既保持上下文，又不多付推理延迟。
pub fn rebuild(pcm: &[i16], segments: &[(usize, usize)]) -> Vec<i16> {
    let mut out: Vec<i16> = Vec::new();
    for &(s, e) in segments {
        let s = s.min(pcm.len());
        let e = e.min(pcm.len());
        if s < e {
            out.extend_from_slice(&pcm[s..e]);
        }
    }
    out
}

/// 裁掉首尾静音，返回 `(新 pcm, 省掉的毫秒数)`。
///
/// 保守原则：**宁可少裁，不可裁掉字**。条件不满足就原样返回。
pub fn trim(pcm: &[i16], cfg: &TrimConfig, sample_rate: i32) -> (Vec<i16>, i64) {
    let frame = (sample_rate as i64 * cfg.frame_ms as i64 / 1000).max(1) as usize;
    if pcm.len() < frame * 2 {
        return (pcm.to_vec(), 0);
    }

    let n = pcm.len() / frame;
    let rms_values: Vec<f64> = (0..n).map(|i| rms(&pcm[i * frame..(i + 1) * frame])).collect();
    let peak = rms_values.iter().copied().fold(0.0f64, f64::max);
    if peak <= 0.0 {
        return (pcm.to_vec(), 0);
    }

    let noise = percentile20(&rms_values);
    let threshold = (peak * cfg.peak_ratio)
        .max(noise * cfg.noise_ratio)
        .max(cfg.abs_floor);

    let active: Vec<usize> = rms_values
        .iter()
        .enumerate()
        .filter(|(_, &v)| v >= threshold)
        .map(|(i, _)| i)
        .collect();
    if active.is_empty() {
        return (pcm.to_vec(), 0);
    }

    let first = active[0];
    let last = *active.last().unwrap();
    let speech = (last - first + 1) * frame;
    let min_speech = (sample_rate as i64 * cfg.min_speech_ms as i64 / 1000) as usize;
    if speech < min_speech {
        return (pcm.to_vec(), 0);
    }

    let lead = (sample_rate as i64 * cfg.lead_ms as i64 / 1000).max(0) as usize;
    let tail = (sample_rate as i64 * cfg.tail_ms as i64 / 1000).max(0) as usize;
    let start = (first * frame).saturating_sub(lead);
    let end = ((last + 1) * frame + tail).min(pcm.len());
    let kept = end.saturating_sub(start);

    // 省不到 min_saved_ratio 就不折腾
    let limit = (pcm.len() as f64) * (1.0 - cfg.min_saved_ratio);
    if kept as f64 >= limit {
        return (pcm.to_vec(), 0);
    }

    let saved_ms = ((pcm.len() - kept) as f64 * 1000.0 / sample_rate as f64).round() as i64;
    (pcm[start..end].to_vec(), saved_ms)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造：静音 + 有声 + 静音，返回 (pcm, 有声段范围)
    fn make_pcm(silent_ms: i32, voiced_ms: i32, sr: i32, amp: i16) -> (Vec<i16>, usize, usize) {
        let silent = (sr as i64 * silent_ms as i64 / 1000) as usize;
        let voiced = (sr as i64 * voiced_ms as i64 / 1000) as usize;
        let mut pcm = vec![0i16; silent];
        let start = pcm.len();
        // 用方波保证能量明显（正负交替，均值仍是 0）
        for i in 0..voiced {
            pcm.push(if i % 2 == 0 { amp } else { -amp });
        }
        let end = pcm.len();
        pcm.extend(std::iter::repeat(0i16).take(silent));
        (pcm, start, end)
    }

    #[test]
    fn energy_segment_finds_voiced_region() {
        let (pcm, vs, ve) = make_pcm(500, 1000, 16000, 3000);
        let cfg = VadConfig::default();
        let segs = segment(&pcm, &cfg);
        assert_eq!(segs.len(), 1, "应切出 1 段: {segs:?}");
        let (s, e) = segs[0];
        // padding 60ms 会向外扩，但必须覆盖有声区
        assert!(s <= vs, "段首 {s} 应不晚于有声起点 {vs}");
        assert!(e >= ve, "段尾 {e} 应不早于有声终点 {ve}");
    }

    #[test]
    fn all_silence_returns_empty() {
        let pcm = vec![0i16; 16000];
        assert!(segment(&pcm, &VadConfig::default()).is_empty());
    }

    #[test]
    fn too_short_pcm_returns_empty() {
        let pcm = vec![1000i16; 100]; // 远短于一帧
        assert!(segment(&pcm, &VadConfig::default()).is_empty());
    }

    #[test]
    fn short_burst_below_min_speech_is_dropped() {
        // 100ms 噪音 < min_speech 250ms → 整段丢弃（防咔哒声）
        let (pcm, _, _) = make_pcm(500, 100, 16000, 3000);
        assert!(segment(&pcm, &VadConfig::default()).is_empty());
    }

    #[test]
    fn probs_hysteresis_needs_start_frames() {
        let cfg = VadConfig::default();
        // 只有 2 帧超阈值 < start_frames(3) → 不进入
        let probs = vec![0.9, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
        assert!(probs_to_segments(&probs, &cfg, 512, None).is_empty());
    }

    #[test]
    fn probs_hysteresis_detects_speech() {
        let cfg = VadConfig::default();
        let mut probs = vec![0.0; 3];
        probs.extend(std::iter::repeat(0.9).take(20));
        probs.extend(std::iter::repeat(0.0).take(10));
        let segs = segment_from_probs(&probs, &cfg, 512, probs.len() * 512);
        assert_eq!(segs.len(), 1, "应切出 1 段: {segs:?}");
    }

    #[test]
    fn probs_end_hysteresis_ignores_single_dip() {
        // 说话中间有一帧掉到 0，不该切断（end_frames=8）
        let cfg = VadConfig::default();
        let mut probs = vec![0.0; 3];
        probs.extend(std::iter::repeat(0.9).take(10));
        probs.push(0.0);
        probs.extend(std::iter::repeat(0.9).take(10));
        probs.extend(std::iter::repeat(0.0).take(10));
        let segs = segment_from_probs(&probs, &cfg, 512, probs.len() * 512);
        assert_eq!(segs.len(), 1, "单帧凹陷不应切段: {segs:?}");
    }

    #[test]
    fn rebuild_concatenates_segments() {
        let pcm: Vec<i16> = (0..100).collect();
        let out = rebuild(&pcm, &[(10, 20), (50, 55)]);
        assert_eq!(out.len(), 15);
        assert_eq!(out[0], 10);
        assert_eq!(out[10], 50);
    }

    #[test]
    fn rebuild_empty_segments_gives_empty() {
        assert!(rebuild(&[1, 2, 3], &[]).is_empty());
    }

    #[test]
    fn rebuild_clamps_out_of_range() {
        let pcm: Vec<i16> = (0..10).collect();
        assert_eq!(rebuild(&pcm, &[(8, 999)]).len(), 2);
        assert!(rebuild(&pcm, &[(20, 30)]).is_empty());
    }

    #[test]
    fn trim_removes_long_silence() {
        // 2s 静音 + 1s 有声 + 2s 静音：应裁掉大部分静音
        let (pcm, _, _) = make_pcm(2000, 1000, 16000, 3000);
        let (out, saved) = trim(&pcm, &TrimConfig::default(), 16000);
        assert!(out.len() < pcm.len(), "应裁短");
        assert!(saved > 0, "应报告省下 {saved}ms");
    }

    #[test]
    fn trim_keeps_short_audio_untouched() {
        // 短于一帧*2 → 原样返回
        let pcm = vec![1000i16; 100];
        let (out, saved) = trim(&pcm, &TrimConfig::default(), 16000);
        assert_eq!(out.len(), pcm.len());
        assert_eq!(saved, 0);
    }

    #[test]
    fn trim_keeps_all_silence_untouched() {
        let pcm = vec![0i16; 16000];
        let (out, saved) = trim(&pcm, &TrimConfig::default(), 16000);
        assert_eq!(out.len(), pcm.len());
        assert_eq!(saved, 0);
    }

    #[test]
    fn trim_skips_when_saving_too_little() {
        // 几乎全是语音，省不到 10% → 不裁
        let (pcm, _, _) = make_pcm(200, 5000, 16000, 3000);
        let (out, saved) = trim(&pcm, &TrimConfig::default(), 16000);
        assert_eq!(out.len(), pcm.len(), "省得少就不该裁");
        assert_eq!(saved, 0);
    }

    #[test]
    fn percentile20_matches_numpy_linear() {
        // numpy.percentile([1,2,3,4,5], 20) == 1.8
        let v = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        assert!((percentile20(&v) - 1.8).abs() < 1e-9);
        let v2 = vec![5.0, 1.0, 3.0]; // 乱序也要对
        assert!((percentile20(&v2) - 1.8).abs() < 1e-9);
        assert_eq!(percentile20(&[7.0]), 7.0);
        assert_eq!(percentile20(&[]), 0.0);
    }
}