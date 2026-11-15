"""PTT 键状态机单测：不碰真麦克风、不碰真键盘，全部靠注入替身。

这里测的是「按下/松开/取消/超时」的判定逻辑 —— 真按键由 Windows 决定，
在自动化环境里也没法模拟（见 docs/05-gotchas.md G14）。
"""

from __future__ import annotations

import ctypes
import time

import numpy as np
import pytest

from voiceime_spike import hotkey


class FakeStream:
    """开流时立刻吐出 4 块音频，模拟回调线程持续供数。"""

    def __init__(self, cb, blocks: int = 4, block: int = 1600) -> None:
        self.cb = cb
        self.blocks = blocks
        self.block = block
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True
        for _ in range(self.blocks):
            self.cb(np.ones(self.block, dtype=np.int16), self.block, None, None)

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def opener_for(holder: dict) -> callable:
    def _open(cb):  # noqa: ANN001
        stream = FakeStream(cb)
        holder["stream"] = stream
        return stream

    return _open


def scripted(states: list[bool]):
    """按顺序返回按键状态，耗尽后一律 False（= 松开）。"""
    it = iter(states)

    def _check() -> bool:
        return next(it, False)

    return _check


def test_hotkey_sides_are_exclusive() -> None:
    """G40：左右互斥——每个预设只认一侧；旧值 ctrl/shift 归一到一个侧。"""
    assert hotkey.KEYS["lctrl"] == (hotkey.VK_LCONTROL,)
    assert hotkey.KEYS["rctrl"] == (hotkey.VK_RCONTROL,)
    assert hotkey.KEYS["lshift"] == (hotkey.VK_LSHIFT,)
    assert hotkey.KEYS["rshift"] == (hotkey.VK_RSHIFT,)
    # 旧值兼容：不再"两侧都触发"
    assert hotkey.KEYS["ctrl"] == (hotkey.VK_RCONTROL,)
    assert hotkey.KEYS["shift"] == (hotkey.VK_LSHIFT,)
    assert hotkey.DEFAULT_KEY == "rctrl"


def test_default_opener_binds_sample_rate(monkeypatch) -> None:
    """回归：`opener=None` 时 MUST 把 sample_rate 绑进去，否则真录音直接 TypeError。"""
    seen: dict = {}

    def fake_open(sample_rate, callback):          # noqa: ANN001
        seen["rate"] = sample_rate
        return FakeStream(callback)

    monkeypatch.setattr(hotkey, "_open_stream", fake_open)
    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        sample_rate=16000,
        poll_ms=1,
        min_hold_ms=0,
        pre_ms=1000,
        is_pressed=scripted([True, True]),
        is_cancelled=lambda: False,
    )
    assert seen["rate"] == 16000
    assert res.ok and res.pcm.size == 4 * 1600


def test_drain_delays_stop_after_release() -> None:
    """松手后必须再收 drain_ms，否则宿主缓冲里的尾音被丢掉（G23）。"""
    t0 = time.perf_counter()
    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=0,
        drain_ms=150,
        opener=opener_for({}),
        is_pressed=scripted([True]),
        is_cancelled=lambda: False,
    )
    elapsed = time.perf_counter() - t0
    assert res.ok
    assert elapsed - res.seconds >= 0.13


def test_cancel_skips_drain() -> None:
    """取消要立刻收工，不能为了捞尾音多等。"""
    t0 = time.perf_counter()
    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=0,
        drain_ms=500,
        opener=opener_for({}),
        is_pressed=lambda: True,
        is_cancelled=lambda: True,
    )
    assert res.cancelled
    assert time.perf_counter() - t0 < 0.3


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValueError):
        hotkey.record_ptt("ctrl+alt", trigger="press", opener=opener_for({}))


def test_normal_hold_records_and_closes_stream() -> None:
    holder: dict = {}
    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=0,
        pre_ms=1000,
        opener=opener_for(holder),
        is_pressed=scripted([True] * 15),
        is_cancelled=lambda: False,
    )
    assert res.ok
    assert not res.cancelled and not res.too_short and not res.truncated
    assert res.pcm.size == 4 * 1600
    assert holder["stream"].started and holder["stream"].stopped and holder["stream"].closed
    # 松开后必须释放设备：live_mic() 只在录音期间非 None
    assert hotkey.live_mic() is None
    assert not hotkey.mic_running()


def test_mic_not_opened_before_key_pressed() -> None:
    """不按键时 MUST 不碰麦克风（用户明确要求：不录音就不占用设备）。"""
    opened: list[object] = []

    def opener(cb):  # noqa: ANN001
        opened.append(cb)
        return FakeStream(cb)

    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=0,
        opener=opener,
        is_pressed=lambda: False,
        should_stop=lambda: True,     # 等按键期间收到退出信号
    )
    assert res.stopped
    assert opened == []               # 连流都没开
    assert hotkey.live_mic() is None


def test_quick_tap_is_flagged_too_short() -> None:
    # Ctrl+C 这类误触：按住几毫秒就松开 → 不许进 ASR
    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=1000,
        opener=opener_for({}),
        is_pressed=scripted([True, True]),
        is_cancelled=lambda: False,
    )
    assert res.too_short
    assert not res.ok


def test_escape_cancels_recording() -> None:
    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=0,
        opener=opener_for({}),
        is_pressed=lambda: True,
        is_cancelled=lambda: True,
    )
    assert res.cancelled
    assert not res.ok


def test_cancelled_short_hold_is_not_also_too_short() -> None:
    """Esc 常在前 200ms 内按下，不能同时被记成「误触」——两者互斥。"""
    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=1000,
        opener=opener_for({}),
        is_pressed=lambda: True,
        is_cancelled=lambda: True,
    )
    assert res.cancelled
    assert not res.too_short
    assert not res.ok


def test_max_seconds_truncates() -> None:
    res = hotkey.record_ptt(
        "rctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=0,
        max_seconds=0.05,
        opener=opener_for({}),
        is_pressed=lambda: True,
        is_cancelled=lambda: False,
    )
    assert res.truncated
    assert 0.04 <= res.seconds < 1.0


def test_wait_for_press_returns_when_key_goes_down() -> None:
    calls = {"n": 0}

    def check() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    hotkey.wait_for_press("ctrl", poll_ms=1, check=check)
    assert calls["n"] == 3


def test_wait_for_press_warns_once_when_nothing_arrives() -> None:
    """等了很久还没按到 → 只提示一次，然后继续等（不能自己退出）。"""
    warned: list[int] = []
    state = {"n": 0}

    def check() -> bool:
        state["n"] += 1
        return state["n"] > 5          # 前 5 次没按，之后「按下了」

    hotkey.wait_for_press(
        "ctrl", poll_ms=1, check=check,
        warn_after=0.0, on_warn=lambda: warned.append(1),
    )
    assert warned == [1]
    assert state["n"] == 6


def test_record_ptt_passes_wait_hint_through() -> None:
    hints: list[int] = []
    res = hotkey.record_ptt(
        "ctrl",
        trigger="press",
        poll_ms=1,
        min_hold_ms=0,
        opener=opener_for({}),
        is_pressed=scripted([True, True]),
        is_cancelled=lambda: False,
        on_wait=lambda: hints.append(1),
    )
    assert res.ok
    assert hints == []       # 立刻就按到了，不该提示


def test_describe_key_mentions_one_side() -> None:
    """G40：描述里不再出现"左右均可"，旧值显示真实生效的那一侧。"""
    assert "左或右" not in hotkey.describe_key("ctrl")
    assert hotkey.describe_key("ctrl") == hotkey.describe_key("rctrl")
    assert "右" in hotkey.describe_key("rctrl")


def test_watch_keys_reports_detected_vk(monkeypatch) -> None:
    monkeypatch.setattr(hotkey, "IS_WINDOWS", True)
    monkeypatch.setattr(hotkey, "ensure_hook", lambda: False)
    monkeypatch.setattr(hotkey, "pressed", lambda vks: vks[0] == 0x41)
    lines: list[str] = []
    hotkey.watch_keys(seconds=0.05, poll_ms=5, emit=lines.append)
    assert any("0x41" in line for line in lines)


def test_watch_keys_reports_nothing_when_key_state_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(hotkey, "IS_WINDOWS", True)
    monkeypatch.setattr(hotkey, "ensure_hook", lambda: False)
    monkeypatch.setattr(hotkey, "pressed", lambda vks: False)
    lines: list[str] = []
    hotkey.watch_keys(seconds=0.05, poll_ms=5, emit=lines.append)
    assert any("没识别到按键" in line for line in lines)


def test_hook_callback_tracks_down_and_up() -> None:
    hook = hotkey.LowLevelHook(allow_injected=True)
    info = hotkey.KBDLLHOOKSTRUCT()
    info.vkCode = hotkey.VK_LCONTROL
    addr = ctypes.addressof(info)

    hook._callback(0, hotkey.WM_KEYDOWN, addr)   # noqa: SLF001
    assert hook.pressed((hotkey.VK_LCONTROL,))
    assert hook.drain_presses() == [hotkey.VK_LCONTROL]

    hook._callback(0, hotkey.WM_KEYUP, addr)     # noqa: SLF001
    assert not hook.pressed((hotkey.VK_LCONTROL,))
    assert hook.event_count() == 2


def test_hook_ignores_own_injected_events_by_magic() -> None:
    """本程序自己打的字带 MAGIC，必须忽略，否则注入会反过来触发 PTT。"""
    hook = hotkey.LowLevelHook()
    info = hotkey.KBDLLHOOKSTRUCT()
    info.vkCode = hotkey.VK_LCONTROL
    info.flags = hotkey.LLKHF_INJECTED
    info.dwExtraInfo = hotkey.EXTRA_INFO_MAGIC
    hook._callback(0, hotkey.WM_KEYDOWN, ctypes.addressof(info))  # noqa: SLF001
    assert not hook.pressed((hotkey.VK_LCONTROL,))
    assert hook.event_count() == 0


def test_hook_accepts_foreign_injected_events() -> None:
    """键盘重映射软件产生的真实按键也是 injected，必须照收（G20）。"""
    hook = hotkey.LowLevelHook()
    info = hotkey.KBDLLHOOKSTRUCT()
    info.vkCode = hotkey.VK_LCONTROL
    info.flags = hotkey.LLKHF_INJECTED
    info.dwExtraInfo = 0
    hook._callback(0, hotkey.WM_KEYDOWN, ctypes.addressof(info))  # noqa: SLF001
    assert hook.pressed((hotkey.VK_LCONTROL,))
    assert hook.event_count() == 1
    assert hook.injected_count() == 1


def test_hook_can_still_filter_all_injected() -> None:
    hook = hotkey.LowLevelHook(allow_injected=False)
    info = hotkey.KBDLLHOOKSTRUCT()
    info.vkCode = hotkey.VK_LCONTROL
    info.flags = hotkey.LLKHF_INJECTED
    info.dwExtraInfo = 0
    hook._callback(0, hotkey.WM_KEYDOWN, ctypes.addressof(info))  # noqa: SLF001
    assert not hook.pressed((hotkey.VK_LCONTROL,))
    assert hook.event_count() == 0


def test_key_name_falls_back_to_hex() -> None:
    assert "Ctrl" in hotkey.key_name(hotkey.VK_LCONTROL)
    assert "0x77" in hotkey.key_name(0x77)
