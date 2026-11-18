"""Rust（`crates/ime-post`）与纯 Python 基准实现的逐字节等价性。

为什么单独一个文件：`test_postprocess.py` 测的是**行为**（黄金用例），跑在当前生效的后端上；
这里测的是**两种实现一致**。任何一边改了规则而另一边没跟，必须在这里红。

前置：`uv run python tools/build_ext.py`（或 `maturin develop`）。没构建过就整体 skip，
保证没有 Rust 环境的机器上 `pytest` 依然是绿的。
"""

from __future__ import annotations

import random

import pytest

from xifeng import postprocess as pp

pytestmark = pytest.mark.skipif(
    pp._rs_process is None,
    reason="未构建 Rust 扩展：uv run python tools/build_ext.py",
)

CONFIGS: list[dict] = [
    {},
    {"filler_level": "off"},
    {"filler_level": "deep"},
    {"standalone": "inject"},
    {"final_punct": "strip"},
    {"repeat_limit": 0},
    {"repeat_limit": 1},
    {"space_between_cjk_latin": False},
    {"filler_level": "deep", "standalone": "inject", "final_punct": "strip"},
]

#: 黄金用例 + 真实 ASR 输出里会出现的形状
CORPUS: list[str] = [
    # 幻觉（G10）
    "谢谢观看",
    "谢谢大家观看。",
    "字幕由志愿者提供。",
    "请不吝点赞、订阅。",
    "请不吝转发",
    "请点赞、关注、转发",
    "明镜与点点",
    "OK",
    "ok.",
    "Ok！",
    "我在视频里听到谢谢观看这句话",
    # 语气词（G5）
    "呃，今天开会。",
    "今天 嗯 开会。",
    "呃 今天 我们 嗯 开个会，讨论一下 PyO3。",
    "呃，嗯，那个……我们开始吧。",
    "他最近呃逆得厉害。",
    "er, I think um, yes",
    "嗯嗯",
    "呃。",
    "，。？！",
    "   ",
    "",
    # 重复与标点
    "好" * 20 + "的",
    "好" * 9,
    "好" * 8,
    "哈哈哈哈",
    "真的吗？？？",
    "真的吗，，。",
    "😂" * 12,
    # 中英混排
    "用PyO3接入",
    "讨论 一下PyO3的接入",
    "跑 PyO3 的 cargo build",
    "API 调用失败率 0.3%",
    # 边界
    "多行\n第二行",
    "结尾带换行\n",
    "  前后都有空格  ",
    "句末标点。",
    "全角空格\u3000分隔",
    "NumPy 和 PyO3",
    "……省略号不处理",
    "1",
]


def _fuzz_corpus(n: int = 150, seed: int = 20261109) -> list[str]:
    """确定性随机串：覆盖混排、边界、重复、空串，避免手写用例的盲区。"""
    rng = random.Random(seed)
    alphabet = [
        "嗯", "呃", "啊", "诶", "呀", "吧", "哦", "的", "会", "好", "开", "天",
        "a", "Z", "9", " ", "  ", "\t", "\n",
        "，", "。", "？", "！", ",", ".", "?", "!", ":", ";", "、",
        "um", "emm", "emmm", "uh", "OK", "ok",
        "PyO3", "API", "😂",
    ]
    out = []
    for _ in range(n):
        k = rng.randint(0, 12)
        out.append("".join(rng.choice(alphabet) for _ in range(k)))
    # 补几条极端形状
    out += ["嗯" * 30, "a" * 12, "？" * 10, "呃" * 5 + "嗯" * 5]
    return out


def _rust_process(raw: str, cfg: pp.PostConfig) -> str | None:
    return pp._rs_process(
        raw,
        cfg.filler_level,
        cfg.standalone,
        cfg.repeat_limit,
        cfg.final_punct,
        cfg.space_between_cjk_latin,
    )


@pytest.mark.parametrize("kwargs", CONFIGS, ids=lambda k: ",".join(f"{a}={b}" for a, b in k.items()) or "default")
def test_process_parity(kwargs: dict) -> None:
    cfg = pp.PostConfig(**kwargs)
    for raw in CORPUS + _fuzz_corpus():
        assert pp.process_py(raw, cfg) == _rust_process(raw, cfg), f"cfg={kwargs} raw={raw!r}"


def test_stats_parity() -> None:
    for raw in CORPUS:
        cleaned = pp.process_py(raw)
        assert pp.stats_py(raw, cleaned) == dict(pp._rs_stats(raw, cleaned)), raw


def test_rust_is_the_active_backend_in_ci() -> None:
    """本文件只在构建过扩展时运行，此时对外入口必须真的走 Rust。"""
    assert pp.backend == "rust"


def test_python_fallback_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟没装扩展的机器：入口回退到纯 Python 且行为不变。"""
    monkeypatch.setattr(pp, "_rs_process", None)
    monkeypatch.setattr(pp, "_rs_stats", None)
    assert pp.process("呃，今天开会。") == "今天开会。"
    assert pp.process("嗯嗯") is None
    assert pp.stats("abc", "ab") == {"chars_in": 3, "chars_out": 2, "dropped": 0}
