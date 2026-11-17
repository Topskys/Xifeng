"""环境自检的判档逻辑。

为什么值得测：判档错会导致 bench 误报 FAIL/PASS，而这个错误会直接误导
"是否回归"的结论。2026-11-09 就因内存换页出现 P95 4480ms 的假回归。
"""

from __future__ import annotations

from xifeng.benv import EnvReport


def _env(cpu: float | None, free_mb: int | None = 8000, total_mb: int | None = 16000,
         commit: int | None = 9000) -> EnvReport:
    return EnvReport(cpu_pct=cpu, mem_free_mb=free_mb, mem_total_mb=total_mb,
                     commit_used_mb=commit)


def test_idle_uses_350_budget() -> None:
    e = _env(cpu=5.0)
    assert e.tier == "idle"
    assert e.asr_budget() == 350
    assert e.trusted()


def test_loaded_uses_650_budget() -> None:
    e = _env(cpu=45.0)
    assert e.tier == "loaded"
    assert e.asr_budget() == 650
    assert e.trusted()


def test_overloaded_is_untrusted() -> None:
    e = _env(cpu=85.0)
    assert e.tier == "overloaded"
    assert not e.trusted()
    assert e.asr_budget() == 350          # 沿用默认值，但结果不可信


def test_low_free_memory_is_pressure_even_when_cpu_idle() -> None:
    # 关键用例：CPU 很闲但内存不足，也会因换页让 P95 虚高
    e = _env(cpu=5.0, free_mb=1200)
    assert e.tier == "pressure"
    assert not e.trusted()
    assert e.mem_pressure


def test_commit_over_physical_is_pressure() -> None:
    # 2026-11-09 实测工况：提交 27.1G > 物理 15.8G
    e = _env(cpu=45.0, free_mb=3000, total_mb=16000, commit=27500)
    assert e.mem_pressure
    assert e.tier == "pressure"
    assert not e.trusted()


def test_unknown_cpu_stays_unknown_and_keeps_default_budget() -> None:
    e = _env(cpu=None)
    assert e.tier == "unknown"
    assert e.asr_budget() == 350
    assert not e.trusted()


def test_line_never_crashes_on_missing_data() -> None:
    e = EnvReport(cpu_pct=None, mem_free_mb=None, mem_total_mb=None, commit_used_mb=None)
    s = e.line()
    assert "CPU ?" in s
    assert "?" in s


def test_line_reports_numbers_when_present() -> None:
    s = _env(cpu=45.4, free_mb=3072, total_mb=16128, commit=27500).line()
    assert "45%" in s
    assert "3.0G" in s
    assert "提交 26.9G" in s or "提交 26.8G" in s
