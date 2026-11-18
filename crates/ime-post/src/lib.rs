//! Xifeng 文本后处理。
//!
//! 这是 `core/xifeng/postprocess.py` 的等价实现，**行为基准是那个 Python 文件**
//! 和 `core/tests/test_postprocess.py` 里的黄金用例（`docs/00-process/plan.md` Step 2）。
//! 任何规则变更 MUST 两边同时改，并由 `core/tests/test_postprocess_parity.py` 证明等价。
//!
//! 设计约束（沿用 Python 侧）：
//! - 纯函数、可幂等：`process(process(x)) == process(x)`
//! - 返回 `None` 表示「不注入」（空 / 幻觉 / 纯语气词）
//! - 中文填充词清理只在含 CJK 时生效，英文里 er/um 是正常词
//! - 不记录、不外传任何文本内容，只暴露长度统计

use regex::Regex;
use std::sync::OnceLock;

/// 需要参与边界判断的标点（与 Python 侧 `PUNCT` 逐字符一致）。
pub const PUNCT: &str = "，。？！、；：,.!?;:";

/// 整句命中即判为幻觉（ASR 在静音/噪声上的典型输出）。
const HALLUCINATION: &[&str] = &[
    r"^谢谢(大家)?观看[。！!.]?$",
    r"^字幕由.*提供[。！!.]?$",
    r"^请不吝(点赞|转发|订阅).*$",
    r"^请(点赞|订阅|关注)[、，,\s]*.*$",
    r"^明镜与点点.*$",
    r"^[Oo][kK][。！!.]?$",
];

/// L1 句首 / L2 句中孤立填充词。长词在前，避免「嗯嗯」被拆成两个「嗯」。
const FILLERS_STANDARD: &[&str] = &[
    "嗯嗯", "emm", "emmm", "嗯", "呃", "啊", "诶", "哎", "唔", "噢", "uh", "um",
];

/// L3 句尾助词：默认不删（「好啊」→「好」会改变语气）。
const FILLERS_DEEP: &[&str] = &["呀", "呢", "吧", "啦", "哦"];

/// 填充词清理力度。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum FillerLevel {
    Off,
    #[default]
    Standard,
    Deep,
}

impl FillerLevel {
    /// 未知取值一律按 `standard`（与 Python 侧一致：只有 `off` / `deep` 是特殊值）。
    pub fn parse(s: &str) -> Self {
        match s {
            "off" => Self::Off,
            "deep" => Self::Deep,
            _ => Self::Standard,
        }
    }
}

/// 整句只剩语气词时的处置。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Standalone {
    /// 丢掉，不注入（默认）
    #[default]
    Drop,
    /// 原样注入
    Inject,
}

impl Standalone {
    pub fn parse(s: &str) -> Self {
        if s == "inject" {
            Self::Inject
        } else {
            Self::Drop
        }
    }
}

/// 句末标点处置。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum FinalPunct {
    #[default]
    Keep,
    Strip,
}

impl FinalPunct {
    pub fn parse(s: &str) -> Self {
        if s == "strip" {
            Self::Strip
        } else {
            Self::Keep
        }
    }
}

/// 后处理配置。字段默认值与 Python 侧 `PostConfig` 一致。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PostConfig {
    pub filler_level: FillerLevel,
    pub standalone: Standalone,
    pub repeat_limit: usize,
    pub final_punct: FinalPunct,
    pub space_between_cjk_latin: bool,
}

impl Default for PostConfig {
    fn default() -> Self {
        Self {
            filler_level: FillerLevel::Standard,
            standalone: Standalone::Drop,
            repeat_limit: 8,
            final_punct: FinalPunct::Keep,
            space_between_cjk_latin: true,
        }
    }
}

// ---------------------------------------------------------------- 编译期缓存

fn hallucination_patterns() -> &'static [Regex] {
    static PATS: OnceLock<Vec<Regex>> = OnceLock::new();
    PATS.get_or_init(|| {
        HALLUCINATION
            .iter()
            .map(|p| Regex::new(p).expect("幻觉正则必须能编译"))
            .collect()
    })
}

fn filler_pattern(level: FillerLevel) -> Option<&'static Regex> {
    static STANDARD: OnceLock<Regex> = OnceLock::new();
    static DEEP: OnceLock<Regex> = OnceLock::new();
    match level {
        FillerLevel::Off => None,
        FillerLevel::Standard => Some(STANDARD.get_or_init(|| build_filler_regex(FILLERS_STANDARD))),
        FillerLevel::Deep => Some(DEEP.get_or_init(|| {
            let mut words: Vec<&str> = FILLERS_STANDARD.to_vec();
            words.extend_from_slice(FILLERS_DEEP);
            build_filler_regex(&words)
        })),
    }
}

/// `(^|[punct\s])(?:alt)([punct\s]|$)`，大小写不敏感，替换为 `$1$2`。
///
/// Python 侧用的是 lookahead `(?=[punct\s]|$)`，Rust `regex` 不支持 lookaround，
/// 这里改成「把右边界吞进匹配、再原样吐回」，对删除操作等价（边界字符不是填充词的一部分）。
fn build_filler_regex(words: &[&str]) -> Regex {
    let mut sorted: Vec<&str> = words.to_vec();
    // 稳定排序（等长词保持原顺序），与 Python `sorted(key=len, reverse=True)` 等价
    sorted.sort_by(|a, b| b.chars().count().cmp(&a.chars().count()));
    let alt = sorted
        .iter()
        .map(|w| regex::escape(w))
        .collect::<Vec<_>>()
        .join("|");
    let class = format!("[{}]", regex::escape(PUNCT));
    Regex::new(&format!(r"(?i)(^|{}|\s)(?:{})({}|\s|$)", class, alt, class))
        .expect("填充词正则必须能编译")
}

/// CJK 判定：与 Python 侧 `[\u4e00-\u9fff]` 一致。
fn has_cjk(text: &str) -> bool {
    text.chars().any(|c| ('\u{4e00}'..='\u{9fff}').contains(&c))
}

// ---------------------------------------------------------------- 各步骤

/// 整句命中幻觉黑名单 → `None`。
pub fn filter_hallucination(text: &str) -> Option<String> {
    let t = text.trim();
    if hallucination_patterns().iter().any(|p| p.is_match(t)) {
        return None;
    }
    Some(text.to_string())
}

/// 单字符连续超过 `limit` 次 → 折叠为 2 次。
///
/// `"哈哈哈哈"` 是真实表意，保留；9 连以上多为 ASR 卡死。
/// 注意阈值：Python 侧是 `(.)\1{limit,}`，即**总数 ≥ limit + 1** 才折叠。
pub fn collapse_repeat(text: &str, limit: usize) -> String {
    if limit == 0 {
        return text.to_string();
    }
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        let mut j = i + 1;
        while j < chars.len() && chars[j] == c {
            j += 1;
        }
        let run = j - i;
        if run > limit {
            out.push(c);
            out.push(c);
        } else {
            for _ in 0..run {
                out.push(c);
            }
        }
        i = j;
    }
    out
}

/// 删除句首 / 句中孤立填充词。`off` 时原样返回。
///
/// 只在词处于边界位置时删除，词中间的「呃」不动（「呃逆」必须保留）。
/// 循环到不动点，覆盖「呃，嗯，那个……」连用。
pub fn remove_fillers(text: &str, level: FillerLevel) -> String {
    if level == FillerLevel::Off || !has_cjk(text) {
        return text.to_string();
    }
    let pat = filler_pattern(level).expect("非 off 档必有正则");
    let mut cur = text.to_string();
    loop {
        let next = pat.replace_all(&cur, "$1$2").to_string();
        if next == cur {
            break;
        }
        cur = next;
    }
    // 删除后可能留下开头的孤立标点/空格：「呃，今天」→「，今天」
    let trimmed = cur.trim_start_matches(|c: char| PUNCT.contains(c) || c == ' ' || c == '\t' || c == '\n');
    trimmed.to_string()
}

/// 中英文补空格、连续标点去重、空白规整、可选去句末标点。
pub fn normalize(text: &str, cfg: &PostConfig) -> String {
    let t = text.replace("\r\n", "\n");
    let t = t.trim();
    // 连续同类标点去重（「，。」不是重复，只有同类重复才折叠）
    let t = collapse_dup_punct(t);
    let t = if cfg.space_between_cjk_latin {
        static CJK_LATIN: OnceLock<Regex> = OnceLock::new();
        static LATIN_CJK: OnceLock<Regex> = OnceLock::new();
        let a = CJK_LATIN.get_or_init(|| {
            Regex::new(r"([\u{4e00}-\u{9fff}])([A-Za-z0-9])").expect("CJK→拉丁 正则")
        });
        let b = LATIN_CJK.get_or_init(|| {
            Regex::new(r"([A-Za-z0-9])([\u{4e00}-\u{9fff}])").expect("拉丁→CJK 正则")
        });
        b.replace_all(&a.replace_all(&t, "$1 $2"), "$1 $2").to_string()
    } else {
        t.to_string()
    };
    // 空白规整：多空格 → 单空格；行首行尾空白去掉
    static SPACES: OnceLock<Regex> = OnceLock::new();
    static LINE: OnceLock<Regex> = OnceLock::new();
    let t = SPACES
        .get_or_init(|| Regex::new(r"[ \t]{2,}").expect("空格 正则"))
        .replace_all(&t, " ")
        .to_string();
    let t = LINE
        .get_or_init(|| Regex::new(r" *\n *").expect("换行 正则"))
        .replace_all(&t, "\n")
        .to_string();
    let t = t.trim().to_string();
    if cfg.final_punct == FinalPunct::Strip {
        t.trim_end_matches(|c: char| PUNCT.contains(c) || c == ' ').to_string()
    } else {
        t
    }
}

/// 连续同类标点折叠成一个（等价于 Python 侧 `([punct])\1+`）。
fn collapse_dup_punct(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut prev: Option<char> = None;
    for c in text.chars() {
        if Some(c) == prev && PUNCT.contains(c) {
            continue;
        }
        out.push(c);
        prev = Some(c);
    }
    out
}

/// 空串或全标点 → `None`。
pub fn final_check(text: &str) -> Option<String> {
    let t = text.trim();
    if t.is_empty() {
        return None;
    }
    if t.chars().all(|c| PUNCT.contains(c) || c.is_whitespace()) {
        return None;
    }
    Some(t.to_string())
}

// ---------------------------------------------------------------- 管线

/// 五步管线，顺序固定。返回 `None` 表示不注入。
pub fn process(raw: &str, cfg: &PostConfig) -> Option<String> {
    let t = filter_hallucination(raw)?;
    let t = collapse_repeat(&t, cfg.repeat_limit);
    let stripped = remove_fillers(&t, cfg.filler_level);
    if stripped.trim().is_empty() {
        // 整句只有语气词：按 standalone 策略处理
        if cfg.standalone == Standalone::Inject && !t.trim().is_empty() {
            return final_check(&normalize(&t, cfg));
        }
        return None;
    }
    final_check(&normalize(&stripped, cfg))
}

/// 给统计页 / 日志用的计数（**不含文本内容**）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stats {
    pub chars_in: usize,
    pub chars_out: usize,
    pub dropped: usize,
}

impl Stats {
    pub fn new(raw: &str, cleaned: Option<&str>) -> Self {
        Self {
            chars_in: raw.chars().count(),
            chars_out: cleaned.map(|c| c.chars().count()).unwrap_or(0),
            dropped: usize::from(cleaned.is_none()),
        }
    }
}

// ---------------------------------------------------------------- 黄金用例

#[cfg(test)]
mod tests {
    use super::*;

    fn p(raw: &str) -> Option<String> {
        process(raw, &PostConfig::default())
    }

    #[test]
    fn hallucination_returns_none() {
        for raw in [
            "谢谢观看",
            "谢谢大家观看。",
            "字幕由志愿者提供。",
            "请不吝点赞、订阅。",
            "OK",
        ] {
            assert_eq!(p(raw), None, "{raw}");
        }
    }

    #[test]
    fn hallucination_only_matches_whole_sentence() {
        assert!(p("我在视频里听到谢谢观看这句话").is_some());
    }

    #[test]
    fn collapse_repeat_over_limit() {
        let raw = format!("{}的", "好".repeat(20));
        assert_eq!(p(&raw).as_deref(), Some("好好的"));
    }

    #[test]
    fn keep_normal_repetition() {
        assert_eq!(p("哈哈哈哈").as_deref(), Some("哈哈哈哈"));
        assert_eq!(p("哈哈").as_deref(), Some("哈哈"));
    }

    /// 阈值边界：9 连触发、8 连不触发（与 `(.)\1{8,}` 对齐）。
    #[test]
    fn collapse_repeat_threshold() {
        assert_eq!(p(&"好".repeat(8)).as_deref(), Some(&*"好".repeat(8)));
        assert_eq!(p(&"好".repeat(9)).as_deref(), Some("好好"));
    }

    #[test]
    fn filler_at_sentence_start() {
        assert_eq!(p("呃，今天开会。").as_deref(), Some("今天开会。"));
    }

    #[test]
    fn filler_in_middle() {
        assert_eq!(p("今天 嗯 开会。").as_deref(), Some("今天 开会。"));
    }

    #[test]
    fn multiple_fillers_removed_in_one_pass() {
        assert_eq!(
            p("呃 今天 我们 嗯 开个会，讨论一下 PyO3。").as_deref(),
            Some("今天 我们 开个会，讨论一下 PyO3。")
        );
    }

    #[test]
    fn filler_inside_word_is_kept() {
        assert_eq!(p("他最近呃逆得厉害。").as_deref(), Some("他最近呃逆得厉害。"));
    }

    #[test]
    fn english_fillers_untouched() {
        assert_eq!(p("er, I think um, yes").as_deref(), Some("er, I think um, yes"));
    }

    #[test]
    fn filler_only_sentence_drops() {
        assert_eq!(p("嗯嗯"), None);
        assert_eq!(p("呃。"), None);
    }

    #[test]
    fn filler_only_sentence_can_be_injected_when_configured() {
        let cfg = PostConfig {
            standalone: Standalone::Inject,
            ..PostConfig::default()
        };
        assert_eq!(process("嗯嗯", &cfg).as_deref(), Some("嗯嗯"));
    }

    #[test]
    fn sentence_final_particles_kept() {
        assert_eq!(p("好啊").as_deref(), Some("好啊"));
        assert_eq!(p("走吧").as_deref(), Some("走吧"));
    }

    #[test]
    fn cjk_latin_spacing() {
        assert_eq!(p("用PyO3接入").as_deref(), Some("用 PyO3 接入"));
        assert_eq!(
            p("讨论 一下PyO3的接入").as_deref(),
            Some("讨论 一下 PyO3 的接入")
        );
    }

    #[test]
    fn duplicate_punctuation_collapsed() {
        assert_eq!(p("真的吗？？？").as_deref(), Some("真的吗？"));
    }

    #[test]
    fn blank_and_punctuation_only_dropped() {
        assert_eq!(p("   "), None);
        assert_eq!(p("。。。"), None);
        assert_eq!(p("，。？！"), None);
    }

    #[test]
    fn final_punct_strip_config() {
        let cfg = PostConfig {
            final_punct: FinalPunct::Strip,
            ..PostConfig::default()
        };
        assert_eq!(process("今天开会。", &cfg).as_deref(), Some("今天开会"));
    }

    #[test]
    fn idempotent() {
        for raw in [
            "呃 今天 我们 嗯 开个会，讨论一下 PyO3 的接入。",
            "用PyO3接入",
            "哈哈哈哈",
            "他最近呃逆得厉害。",
        ] {
            let once = p(raw).expect("首轮不该被丢弃");
            assert_eq!(p(&once).as_deref(), Some(once.as_str()), "{raw}");
        }
    }

    #[test]
    fn stats_never_exposes_text() {
        let raw = "呃 今天开会";
        let cleaned = p(raw);
        let s = Stats::new(raw, cleaned.as_deref());
        assert_eq!(s.chars_in, 6);
        assert_eq!(s.dropped, 0);
        assert!(s.chars_out > 0);
    }

    #[test]
    fn filler_level_off_keeps_them() {
        let cfg = PostConfig {
            filler_level: FillerLevel::Off,
            ..PostConfig::default()
        };
        assert_eq!(process("呃，今天开会。", &cfg).as_deref(), Some("呃，今天开会。"));
    }

    #[test]
    fn filler_level_deep_drops_tail_particles() {
        let cfg = PostConfig {
            filler_level: FillerLevel::Deep,
            ..PostConfig::default()
        };
        // 深档才删句尾助词；且仍受「必须处于边界」约束，
        // 「好啊」不动、「好 啊」→「好」（与 Python 行为一致）
        assert_eq!(process("好 啊", &cfg).as_deref(), Some("好"));
        assert_eq!(process("好啊", &cfg).as_deref(), Some("好啊"));
    }
}
