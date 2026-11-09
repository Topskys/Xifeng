"""Trace：一次输入的阶段耗时记录。

字段口径见 docs/04-reference/metrics.md。M0 只输出到日志/JSONL，不落 SQLite。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class Outcome(str, Enum):
    INJECTED = "Injected"
    CANCELLED = "Cancelled"
    DROPPED = "Dropped"
    FAILED = "Failed"


@dataclass
class Trace:
    id: int = 0
    ts: str = ""
    mode: str = "ptt"
    outcome: Outcome = Outcome.FAILED

    # 各阶段耗时（ms）
    ms_asr: int = 0
    ms_vad: int = 0
    ms_post: int = 0              # 清理 + 纠错（纠错单独记在 ms_correct）
    ms_correct: int = 0
    ms_inject: int = 0
    e2e_ms: int = 0

    # 文本与音频信息（不含全文，隐私约束）
    chars: int = 0
    speech_ms: int = 0
    trimmed_ms: int = 0            # 首尾静音裁掉的毫秒数
    ms_trim: int = 0               # 裁剪本身的耗时
    truncated: bool = False
    dropped_segments: int = 0
    fixes: int = 0                 # 纠错采纳的修改处数（不含文本内容）

    injection_strategy: str = "unicode"
    error: str = ""

    _t0: float = field(default=0.0, repr=False, compare=False)

    def start(self) -> "Trace":
        self._t0 = time.perf_counter()
        self.ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        return self

    def stop(self) -> "Trace":
        if self._t0:
            self.e2e_ms = int((time.perf_counter() - self._t0) * 1000)
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_t0", None)
        d["outcome"] = self.outcome.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class TraceSink:
    """追加写 JSONL。写失败不影响主流程。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def write(self, trace: Trace) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(trace.to_json() + "\n")
        except OSError:
            pass


def percentiles(values: list[int]) -> dict[str, int]:
    """返回 p50/p95/p99。空列表返回全 0。"""
    if not values:
        return {"p50": 0, "p95": 0, "p99": 0}
    s = sorted(values)
    n = len(s)

    def pick(p: float) -> int:
        # 最近秩法（nearest-rank）：p95 表示"95% 的样本不超过它"
        import math

        idx = min(n - 1, max(0, math.ceil(p * n) - 1))
        return s[idx]

    return {"p50": pick(0.50), "p95": pick(0.95), "p99": pick(0.99)}
