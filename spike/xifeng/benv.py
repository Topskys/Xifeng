"""测量环境自检：CPU 负载、内存余量。

为什么需要：ASR 的尾部延迟对物理内存余量极其敏感。2026-11-09 实测同一段音频、
同一份代码，CPU 38% 时空闲内存 3GB，P95=589ms；CPU 57% 且提交内存 27.1G > 物理
15.8G 时，P95 飙到 4480ms（换页）。若不记录环境，bench 数字不可比，会误判成代码回归。

判档口径见 docs/01-overview/latency-budget.md：
    空载（CPU < 10%）：ASR 预算 350ms
    带载（CPU 10–70%）：ASR 预算 650ms
    高载（CPU > 70%）或内存紧张：数字不可信，只作参考
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class EnvReport:
    cpu_pct: float | None
    mem_free_mb: int | None
    mem_total_mb: int | None
    commit_used_mb: int | None

    @property
    def mem_free_gb(self) -> float | None:
        return None if self.mem_free_mb is None else self.mem_free_mb / 1024

    @property
    def mem_pressure(self) -> bool:
        """物理内存余量 < 2GB，或提交量超过物理内存 1.2 倍（换页风险）。"""
        if self.mem_free_mb is None:
            return False
        if self.mem_free_mb < 2 * 1024:
            return True
        if self.commit_used_mb and self.mem_total_mb:
            return self.commit_used_mb > self.mem_total_mb * 1.2
        return False

    @property
    def tier(self) -> str:
        """idle | loaded | overloaded | pressure | unknown"""
        if self.tier_unknown:
            return "unknown"
        if self.mem_pressure:
            return "pressure"
        if self.cpu_pct is None:
            return "unknown"
        if self.cpu_pct > 70:
            return "overloaded"
        if self.cpu_pct < 10:
            return "idle"
        return "loaded"

    @property
    def tier_unknown(self) -> bool:
        return self.cpu_pct is None

    def asr_budget(self, default: int = 350) -> int:
        """按档位给出 ASR 预算；未知环境沿用传入的默认值。"""
        if self.tier == "idle":
            return 350
        if self.tier == "loaded":
            return 650
        return default

    def trusted(self) -> bool:
        return self.tier in ("idle", "loaded")

    def line(self) -> str:
        cpu = "?" if self.cpu_pct is None else f"{self.cpu_pct:.0f}%"
        free = "?" if self.mem_free_gb is None else f"{self.mem_free_gb:.1f}G"
        total = "?" if self.mem_total_mb is None else f"{self.mem_total_mb / 1024:.1f}G"
        commit = "" if self.commit_used_mb is None else f" 提交 {self.commit_used_mb / 1024:.1f}G"
        return f"CPU {cpu} · 空闲内存 {free}/{total}{commit}"


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory() -> tuple[int | None, int | None, int | None]:
    """返回 (free_mb, total_mb, commit_used_mb)。非 Windows 或无权限时全 None。"""
    if sys.platform != "win32":
        return None, None, None
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None, None, None
        free_mb = int(st.ullAvailPhys // (1024 * 1024))
        total_mb = int(st.ullTotalPhys // (1024 * 1024))
        commit_mb = int((st.ullTotalPageFile - st.ullAvailPageFile) // (1024 * 1024))
        return free_mb, total_mb, commit_mb
    except Exception:
        return None, None, None


def _cpu_sample() -> float | None:
    """用 GetSystemTimes 取一次瞬时使用率（两次采样间隔 200ms）。"""
    if sys.platform != "win32":
        return None

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    def _ft(v: "FILETIME") -> int:
        return (v.dwHighDateTime << 32) | v.dwLowDateTime

    try:
        kernel32 = ctypes.windll.kernel32

        def snapshot() -> tuple[int, int, int]:
            idle, kern, user = FILETIME(), FILETIME(), FILETIME()
            kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
            return _ft(idle), _ft(kern), _ft(user)

        i1, k1, u1 = snapshot()
        import time as _t

        _t.sleep(0.2)
        i2, k2, u2 = snapshot()
        idle_d = i2 - i1
        total_d = (k2 - k1) + (u2 - u1)
        if total_d <= 0:
            return None
        return max(0.0, min(100.0, (1.0 - idle_d / total_d) * 100.0))
    except Exception:
        return None


def probe() -> EnvReport:
    cpu = _cpu_sample()
    free, total, commit = _memory()
    return EnvReport(cpu_pct=cpu, mem_free_mb=free, mem_total_mb=total, commit_used_mb=commit)


TIER_NOTE = {
    "idle": "空载：ASR 预算按 350ms",
    "loaded": "带载：ASR 预算按 650ms（带载口径见 latency-budget.md §3）",
    "overloaded": "高载：本组数字不可信，只作参考，勿据此判定回归",
    "pressure": "内存紧张（换页风险）：P95/P99 会虚高，本组数字不可信",
    "unknown": "无法读取环境：判定沿用默认预算",
}
