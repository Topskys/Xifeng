//! VoiceIME 的 PyO3 扩展模块 `voiceime_rs`。
//!
//! 只做「Python 签名 ↔ Rust 实现」的转接，规则本身在 `ime-post`。
//! 参数命名与 `spike/voiceime_spike/postprocess.py` 的 `PostConfig` 字段一一对应，
//! 这样 Python 侧不必维护两套配置结构。

use std::collections::HashMap;

use ime_post::{FinalPunct, FillerLevel, PostConfig, Standalone};
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

#[pymodule]
fn voiceime_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(process, m)?)?;
    m.add_function(wrap_pyfunction!(filter_hallucination, m)?)?;
    m.add_function(wrap_pyfunction!(collapse_repeat, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    Ok(())
}
