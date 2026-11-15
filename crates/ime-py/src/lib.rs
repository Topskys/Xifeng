//! VoiceIME 的 PyO3 扩展模块 `voiceime_rs`。
//!
//! 只做「Python 签名 ↔ Rust 实现」的转接，规则本身在 `ime-post`。
//! 参数命名与 `spike/voiceime_spike/postprocess.py` 的 `PostConfig` 字段一一对应，
//! 这样 Python 侧不必维护两套配置结构。

use std::collections::HashMap;

use ime_post::{FinalPunct, FillerLevel, PostConfig, Standalone};
use ime_vad::{TrimConfig, VadConfig};
use pyo3::prelude::*;

/// 五步后处理管线。返回 `None` 表示「不注入」。
#[pyfunction]
#[pyo3(signature = (
    raw,
    filler_level = "standard",
    standalone = "drop",
    repeat_limit = 8,
    final_punct = "keep",
    space_between_cjk_latin = true
))]
#[allow(clippy::too_many_arguments)]
fn process(
    raw: &str,
    filler_level: &str,
    standalone: &str,
    repeat_limit: usize,
    final_punct: &str,
    space_between_cjk_latin: bool,
) -> Option<String> {
    let cfg = PostConfig {
        filler_level: FillerLevel::parse(filler_level),
        standalone: Standalone::parse(standalone),
        repeat_limit,
        final_punct: FinalPunct::parse(final_punct),
        space_between_cjk_latin,
    };
    ime_post::process(raw, &cfg)
}

/// 低层步骤：只做幻觉过滤（便于单独验证）。
#[pyfunction]
fn filter_hallucination(text: &str) -> Option<String> {
    ime_post::filter_hallucination(text)
}

/// 低层步骤：连续字符折叠。
#[pyfunction]
#[pyo3(signature = (text, limit = 8))]
fn collapse_repeat(text: &str, limit: usize) -> String {
    ime_post::collapse_repeat(text, limit)
}

/// 统计计数。**只有长度与 outcome，不含任何文本**（日志红线）。
#[pyfunction]
fn stats(raw: &str, cleaned: Option<&str>) -> HashMap<&'static str, usize> {
    let s = ime_post::Stats::new(raw, cleaned);
    HashMap::from([
        ("chars_in", s.chars_in),
        ("chars_out", s.chars_out),
        ("dropped", s.dropped),
    ])
}

/// 从 Python 关键字参数构造 VadConfig（默认值与 Python 侧一致）。
fn vad_cfg(
    sample_rate: i32,
    frame_ms: i32,
    start_ratio: f64,
    end_ratio: f64,
    start_prob: f64,
    end_prob: f64,
    start_frames: usize,
    end_frames: usize,
    min_speech_ms: i32,
    merge_gap_ms: i32,
    pad_ms: i32,
    head_pad_ms: i32,
    tail_pad_ms: i32,
) -> VadConfig {
    VadConfig {
        sample_rate,
        frame_ms,
        start_ratio,
        end_ratio,
        start_prob,
        end_prob,
        start_frames,
        end_frames,
        min_speech_ms,
        merge_gap_ms,
        pad_ms,
        head_pad_ms,
        tail_pad_ms,
    }
}

/// `numpy int16 一维数组` → `Vec<i16>`。
///
/// 走 buffer 协议而不是 `extract::<Vec<u8>>()`：后者会把数组当整数序列逐个取，
/// 3000 这类值直接溢出（OverflowError），长度也会翻倍。
fn pcm_from_py(pcm: &Bound<'_, PyAny>) -> PyResult<Vec<i16>> {
    use pyo3::types::PyBytes;
    // 直接传 bytes 时最高效；numpy 数组走 .tobytes()
    if let Ok(py_bytes) = pcm.cast::<PyBytes>() {
        return to_i16(py_bytes.as_bytes());
    }
    let buf = pcm.call_method0("tobytes")?;
    let bytes: Vec<u8> = buf.extract()?;
    to_i16(&bytes)
}

fn to_i16(bytes: &[u8]) -> PyResult<Vec<i16>> {
    if bytes.len() % 2 != 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "pcm 字节数必须是偶数（int16）",
        ));
    }
    Ok(bytes
        .chunks_exact(2)
        .map(|c| i16::from_le_bytes([c[0], c[1]]))
        .collect())
}

fn pcm_to_py(py: Python<'_>, pcm: &[i16]) -> PyResult<Py<PyAny>> {
    let mut bytes = Vec::with_capacity(pcm.len() * 2);
    for v in pcm {
        bytes.extend_from_slice(&v.to_le_bytes());
    }
    let np = py.import("numpy")?;
    let arr = np.call_method1("frombuffer", (bytes, "int16"))?;
    Ok(arr.unbind())
}

/// 能量法切分。返回 `[(start, end), ...]`。
#[pyfunction]
#[pyo3(signature = (
    pcm, sample_rate = 16000, frame_ms = 30, start_ratio = 0.5, end_ratio = 0.35,
    start_prob = 0.5, end_prob = 0.35, start_frames = 3, end_frames = 8,
    min_speech_ms = 250, merge_gap_ms = 200, pad_ms = 60,
    head_pad_ms = 300, tail_pad_ms = 300
))]
#[allow(clippy::too_many_arguments)]
fn vad_segment(
    pcm: &Bound<'_, PyAny>,
    sample_rate: i32,
    frame_ms: i32,
    start_ratio: f64,
    end_ratio: f64,
    start_prob: f64,
    end_prob: f64,
    start_frames: usize,
    end_frames: usize,
    min_speech_ms: i32,
    merge_gap_ms: i32,
    pad_ms: i32,
    head_pad_ms: i32,
    tail_pad_ms: i32,
) -> PyResult<Vec<(usize, usize)>> {
    let cfg = vad_cfg(
        sample_rate, frame_ms, start_ratio, end_ratio, start_prob, end_prob,
        start_frames, end_frames, min_speech_ms, merge_gap_ms, pad_ms,
        head_pad_ms, tail_pad_ms,
    );
    let data = pcm_from_py(pcm)?;
    Ok(ime_vad::segment(&data, &cfg))
}

/// 概率流 → 语音段（双阈值滞回 + 后处理）。
#[pyfunction]
#[pyo3(signature = (
    probs, window = 512, total_samples = 0,
    sample_rate = 16000, frame_ms = 30, start_ratio = 0.5, end_ratio = 0.35,
    start_prob = 0.5, end_prob = 0.35, start_frames = 3, end_frames = 8,
    min_speech_ms = 250, merge_gap_ms = 200, pad_ms = 60,
    head_pad_ms = 300, tail_pad_ms = 300
))]
#[allow(clippy::too_many_arguments)]
fn vad_segment_from_probs(
    probs: Vec<f64>,
    window: usize,
    total_samples: usize,
    sample_rate: i32,
    frame_ms: i32,
    start_ratio: f64,
    end_ratio: f64,
    start_prob: f64,
    end_prob: f64,
    start_frames: usize,
    end_frames: usize,
    min_speech_ms: i32,
    merge_gap_ms: i32,
    pad_ms: i32,
    head_pad_ms: i32,
    tail_pad_ms: i32,
) -> Vec<(usize, usize)> {
    let cfg = vad_cfg(
        sample_rate, frame_ms, start_ratio, end_ratio, start_prob, end_prob,
        start_frames, end_frames, min_speech_ms, merge_gap_ms, pad_ms,
        head_pad_ms, tail_pad_ms,
    );
    let total = if total_samples == 0 {
        probs.len() * window
    } else {
        total_samples
    };
    ime_vad::segment_from_probs(&probs, &cfg, window, total)
}

/// 把多个语音段拼回单条 PCM。
#[pyfunction]
fn vad_rebuild(py: Python<'_>, pcm: &Bound<'_, PyAny>, segments: Vec<(usize, usize)>) -> PyResult<Py<PyAny>> {
    let data = pcm_from_py(pcm)?;
    let out = ime_vad::rebuild(&data, &segments);
    pcm_to_py(py, &out)
}

/// 裁掉首尾静音，返回 `(pcm, 省掉的毫秒数)`。
#[pyfunction]
#[pyo3(signature = (
    pcm, sample_rate = 16000, frame_ms = 30, peak_ratio = 0.05, noise_ratio = 3.0,
    abs_floor = 200.0, lead_ms = 200, tail_ms = 300, min_speech_ms = 250,
    min_saved_ratio = 0.10
))]
#[allow(clippy::too_many_arguments)]
fn vad_trim(
    py: Python<'_>,
    pcm: &Bound<'_, PyAny>,
    sample_rate: i32,
    frame_ms: i32,
    peak_ratio: f64,
    noise_ratio: f64,
    abs_floor: f64,
    lead_ms: i32,
    tail_ms: i32,
    min_speech_ms: i32,
    min_saved_ratio: f64,
) -> PyResult<(Py<PyAny>, i64)> {
    let cfg = TrimConfig {
        frame_ms,
        peak_ratio,
        noise_ratio,
        abs_floor,
        lead_ms,
        tail_ms,
        min_speech_ms,
        min_saved_ratio,
    };
    let data = pcm_from_py(pcm)?;
    let (out, saved) = ime_vad::trim(&data, &cfg, sample_rate);
    Ok((pcm_to_py(py, &out)?, saved))
}

#[pymodule]
fn voiceime_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(process, m)?)?;
    m.add_function(wrap_pyfunction!(filter_hallucination, m)?)?;
    m.add_function(wrap_pyfunction!(collapse_repeat, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    m.add_function(wrap_pyfunction!(vad_segment, m)?)?;
    m.add_function(wrap_pyfunction!(vad_segment_from_probs, m)?)?;
    m.add_function(wrap_pyfunction!(vad_rebuild, m)?)?;
    m.add_function(wrap_pyfunction!(vad_trim, m)?)?;
    Ok(())
}
