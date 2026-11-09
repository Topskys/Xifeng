"""TextPostProcessor：ASR 输出到注入之间的文本清理。

规则来源：spec.md §4.3 + docs/05-gotchas.md G5/G10。
必须在 Rust 侧等价实现（plan.md Step 2），本文件是行为基准。

设计要点：
- 纯函数，可幂等：process(process(x)) == process(x)
- 返回 None 表示"不注入"（空/幻觉/纯语气词）
- 中文填充词清理仅在含 CJK 时生效（英文里 er/um 是正常词）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 词表

HALLUCINATION_PATTERNS = [
    r"^谢谢(大家)?观看[。！!.]?$",
    r"^字幕由.*提供[。！!.]?$",
    r"^请不吝(点赞|转发|订阅).*$",
    r"^请(点赞|订阅|关注)[、，,\s]*.*$",
    r"^明镜与点点.*$",
    r"^[Oo][kK][。！!.]?$",  # 纯 "OK" 常是误触噪声，M0 先按幻觉处理
]

# L1 句首 / L2 句中孤立填充词。长词在前，避免 "嗯嗯" 被拆成两个 "嗯"。
FILLERS_STANDARD = ["嗯嗯", "emm", "emmm", "嗯", "呃", "啊", "诶", "哎", "唔", "噢", "uh", "um"]

# L3 句尾助词：默认不删（"好啊"→"好" 会改变语气）。
FILLERS_DEEP = ["呀", "呢", "吧", "啦", "哦"]

PUNCT = "，。？！、；：,.!?;:"
_BOUNDARY_L = rf"(^|[{re.escape(PUNCT)}\s])"
_BOUNDARY_R = rf"(?=[{re.escape(PUNCT)}\s]|$)"


@dataclass(frozen=True)
class PostConfig:
    filler_level: str = "standard"  # off | standard | deep
    standalone: str = "drop"        # drop | inject
    repeat_limit: int = 8
    final_punct: str = "keep"       # keep | strip
    space_between_cjk_latin: bool = True


_CJK = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------- 步骤

def filter_hallucination(text: str) -> str | None:
    """整句命中幻觉黑名单 → None。"""
    t = text.strip()
    for pat in HALLUCINATION_PATTERNS:
        if re.match(pat, t):
            return None
    return text


def collapse_repeat(text: str, limit: int = 8) -> str:
    """单个字符连续超过 limit 次 → 折叠为 2 次。

    "哈哈"、"哈哈哈哈" 是真实表意，保留；9 连以上多为 ASR 卡死。
    """
    if limit < 1:
        return text
    return re.sub(rf"(.)\1{{{limit},}}", r"\1\1", text, flags=re.DOTALL)


def _filler_pattern(words: list[str]) -> re.Pattern[str]:
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(_BOUNDARY_L + rf"(?:{alt})" + _BOUNDARY_R, re.IGNORECASE)


_FILLER_CACHE: dict[str, re.Pattern[str]] = {}


def _patterns_for(level: str) -> re.Pattern[str]:
    """正则编译一次就够；不缓存会让首次调用的耗时混进延迟统计。"""
    pat = _FILLER_CACHE.get(level)
    if pat is None:
        words = list(FILLERS_STANDARD)
        if level == "deep":
            words += FILLERS_DEEP
        pat = _filler_pattern(words)
        _FILLER_CACHE[level] = pat
    return pat


def remove_fillers(text: str, level: str = "standard") -> str | None:
    """删除句首/句中孤立填充词。level=off 时原样返回。

    只在词处于边界位置时删除，词中间的「呃」不动（"呃逆" 必须保留）。
    循环到不动点，覆盖 "呃，嗯，那个……" 连用。
    """
    if level == "off":
        return text
    if not _CJK.search(text):
        return text  # 英文场景跳过，避免误删 "er"/"um"

    pat = _patterns_for(level)
    prev = None
    while prev != text:
        prev = text
        text = pat.sub(r"\1", text)

    # 删除后可能留下开头的孤立标点/空格："呃，今天" → "，今天"
    text = text.lstrip(PUNCT + " \t\n")
    return text


def normalize(text: str, cfg: PostConfig) -> str:
    """中英文补空格、连续标点去重、空白规整、可选去句末标点。"""
    t = text.replace("\r\n", "\n").strip()

    # 连续标点去重（"，。" 不是重复，只有同类重复才折叠）
    t = re.sub(rf"([{re.escape(PUNCT)}])\1+", r"\1", t)

    if cfg.space_between_cjk_latin:
        t = re.sub(r"([\u4e00-\u9fff])([A-Za-z0-9])", r"\1 \2", t)
        t = re.sub(r"([A-Za-z0-9])([\u4e00-\u9fff])", r"\1 \2", t)

    # 空白规整：多空格 → 单空格；行首行尾空白去掉
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = t.strip()

    if cfg.final_punct == "strip":
        t = t.rstrip(PUNCT + " ")
    return t


def final_check(text: str) -> str | None:
    """空串或全标点 → None。"""
    t = text.strip()
    if not t:
        return None
    if all(ch in PUNCT or ch.isspace() for ch in t):
        return None
    return t


# ---------------------------------------------------------------- 管线

def process(raw: str, cfg: PostConfig | None = None) -> str | None:
    """五步管线，顺序固定。返回 None 表示不注入。"""
    cfg = cfg or PostConfig()

    t = filter_hallucination(raw)
    if t is None:
        return None

    t = collapse_repeat(t, cfg.repeat_limit)

    stripped = remove_fillers(t, cfg.filler_level)
    if stripped is None:
        return None
    if not stripped.strip():
        # 整句只有语气词：按 standalone 策略处理
        if cfg.standalone == "inject" and t.strip():
            return final_check(normalize(t, cfg))
        return None
    t = stripped

    t = normalize(t, cfg)
    return final_check(t)


def stats(raw: str, cleaned: str | None) -> dict[str, int]:
    """给统计页/日志用的计数（不含文本内容）。"""
    return {
        "chars_in": len(raw),
        "chars_out": len(cleaned) if cleaned else 0,
        "dropped": 1 if cleaned is None else 0,
    }
