"""PTT 主循环的分支行为：取消/误触/超时都必须走对路径。

不加载模型、不碰麦克风、不碰真键盘 —— 全部靠替身。
"""

from __future__ import annotations

import numpy as np

import run_spike
from xifeng import hotkey


def ptt_result(**kw) -> hotkey.PttResult:
    base = {"pcm": np.ones(1600, dtype=np.int16), "seconds": 1.0}
    base.update(kw)
    return hotkey.PttResult(**base)


def test_cancelled_and_too_short_are_skipped(monkeypatch) -> None:
    injected: list[int] = []

    def fake_report(  # noqa: ANN001
        pipe, pcm, args, store=None, sample_rate=16000, mode="ptt"
    ) -> bool:
        assert store is None and mode == "ptt"
        injected.append(int(pcm.size))
        return True

    seq = iter([
        ptt_result(cancelled=True),
        ptt_result(too_short=True, seconds=0.1),
        ptt_result(pcm=np.ones(3200, dtype=np.int16)),
    ])
    monkeypatch.setattr(run_spike, "report", fake_report)
    monkeypatch.setattr(hotkey, "record_ptt", lambda *a, **k: next(seq))

    args = run_spike.parse_args(["--ptt", "--times", "3", "--no-inject"])
    rc = run_spike.ptt_loop(object(), args)

    assert injected == [3200]      # 只有第三段真的进了管线
    assert rc == 1                 # 1/3 成功 → 非零


def test_all_success_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(run_spike, "report", lambda *a, **k: True)
    monkeypatch.setattr(hotkey, "record_ptt", lambda *a, **k: ptt_result())

    args = run_spike.parse_args(["--ptt", "--times", "2", "--no-inject"])
    assert run_spike.ptt_loop(object(), args) == 0


def test_truncated_still_processed(monkeypatch) -> None:
    monkeypatch.setattr(run_spike, "report", lambda *a, **k: True)
    monkeypatch.setattr(
        hotkey, "record_ptt", lambda *a, **k: ptt_result(seconds=60.0, truncated=True)
    )

    args = run_spike.parse_args(["--ptt", "--times", "1", "--no-inject"])
    assert run_spike.ptt_loop(object(), args) == 0


def test_key_choice_is_passed_through(monkeypatch) -> None:
    seen: list[str] = []

    def fake_record(key, **kw):  # noqa: ANN001
        seen.append(key)
        return ptt_result()

    monkeypatch.setattr(run_spike, "report", lambda *a, **k: True)
    monkeypatch.setattr(hotkey, "record_ptt", fake_record)

    args = run_spike.parse_args(["--ptt", "--times", "1", "--key", "rctrl", "--no-inject"])
    run_spike.ptt_loop(object(), args)
    assert seen == ["rctrl"]


# ---------------------------------------------------------------- 双击触发

class _FakeStream:
    def __init__(self, cb) -> None:  # noqa: ANN001
        self.cb = cb

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def _run_ptt(intervals, other_schedule=(), tap_ms=300, gap_ms=500, max_seconds=1.5):
    """按时间线驱动 record_ptt（双击触发）。

    intervals: [(down_s, up_s), ...] —— Ctrl 按下的时间段（秒）；
    other_schedule: [(start_s, end_s, [vk...]), ...] —— 其它键按下的时间窗。
    is_pressed / other_keys 都由"当前时刻落在哪个区间"决定，与真实键盘一致。
    """
    state = {"t": 0.0, "poll": 0}
    holder: dict = {}

    def clock() -> float:
        # MUST 返回秒（与 perf_counter 同单位）
        state["t"] += 0.01
        return state["t"]

    def opener(cb) -> None:  # noqa: ANN001
        holder["cb"] = cb
        return _FakeStream(cb)

    def check() -> bool:
        t = state["t"]
        return any(a <= t < b for a, b in intervals)

    def other_keys() -> list[int]:
        state["t"] += 0.01   # 每次轮询推进 10ms：录音循环不调 clock，时间窗靠这里推进
        cb = holder.get("cb")
        if cb is not None:
            import numpy as np

            cb(np.zeros((160, 1), dtype=np.int16), None, None, None)
        t = state["t"]
        for start, end, vks in other_schedule:
            if start <= t < end:
                return vks
        return []

    return hotkey.record_ptt(
        "ctrl",
        min_hold_ms=0,
        max_seconds=max_seconds,
        should_stop=lambda: state["t"] > END + 0.5,
        poll_ms=10,
        is_pressed=check,
        is_cancelled=lambda: False,
        other_keys=other_keys,
        opener=opener,
        tap_ms=tap_ms,
        gap_ms=gap_ms,
        clock=clock,
    )


END = 10.0   # 所有时间线的"最后期限"：超出的等待靠 stop 信号收尾


def test_double_tap_hold_records() -> None:
    """双击后保持长按 → 正常录音（tap1 30ms，gap 内 100ms 按下 tap2，长按）。"""
    res = _run_ptt([(0.0, 0.03), (0.1, END)])
    assert res.combo is False
    assert res.pcm.size > 0


def test_single_press_held_does_not_trigger() -> None:
    """单击按住不放（无第二次点击）→ 永不进入录音，超时收尾。"""
    res = _run_ptt([(0.0, END)])
    assert res.pcm.size == 0
    assert not res.ok


def test_combo_during_tap1_does_not_trigger() -> None:
    """tap1 期间按了 C 键 → 作废；tap2 照常也无法触发（其它键持续干扰）。"""
    res = _run_ptt([(0.0, 0.03), (0.1, END)], other_schedule=[(0.01, 0.03, [0x43])])
    assert res.pcm.size == 0


def test_combo_during_hold_aborts_recording() -> None:
    """双击保持录音期间按下 C → combo=True，整段丢弃。"""
    res = _run_ptt([(0.0, 0.03), (0.1, END)], other_schedule=[(0.2, END, [0x43])])
    assert res.combo is True
    assert not res.ok


def test_gap_timeout_does_not_trigger() -> None:
    """两次点击间隔 > gap_ms → 不触发。"""
    res = _run_ptt([(0.0, 0.03), (0.9, END)], gap_ms=500)
    assert res.pcm.size == 0
