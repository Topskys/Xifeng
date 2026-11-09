"""注入的纯逻辑部分：码元切分、换行映射、分块。

不需要真实窗口，任何平台都能跑。真正"打到记事本里"属于手测（spec A0-1/A0-6）。
"""

from __future__ import annotations

import pytest

from voiceime_spike import inject as inj


def test_chinese_characters_one_unit_each() -> None:
    events = inj.build_events("你好")
    assert events == [("text", ord("你")), ("text", ord("好"))]


def test_emoji_split_into_surrogate_pair() -> None:
    # 😀 = U+1F600 = 2 个 UTF-16 码元，按 chars() 发会乱码（G2）
    events = inj.build_events("😀")
    assert len(events) == 2
    assert all(kind == "text" for kind, _ in events)
    hi, lo = events[0][1], events[1][1]
    assert 0xD800 <= hi <= 0xDBFF
    assert 0xDC00 <= lo <= 0xDFFF


def test_newline_becomes_vk_return() -> None:
    events = inj.build_events("a\nb")
    assert events[1] == ("key", inj.VK_RETURN)


def test_crlf_normalized_to_single_return() -> None:
    events = inj.build_events("a\r\nb")
    assert [e for e in events if e[0] == "key"] == [("key", inj.VK_RETURN)]


def test_tab_becomes_vk_tab() -> None:
    assert inj.build_events("\t") == [("key", 0x09)]


def test_empty_text_has_no_events() -> None:
    assert inj.build_events("") == []


def test_chunking_respects_size() -> None:
    events = inj.build_events("一" * 70)
    chunks = inj.chunked(events, 32)
    assert [len(c) for c in chunks] == [32, 32, 6]


def test_chunking_with_invalid_size_returns_single_chunk() -> None:
    events = inj.build_events("abc")
    assert inj.chunked(events, 0) == [events]
    assert inj.chunked([], 32) == []


def test_inject_empty_text_is_noop() -> None:
    pytest.importorskip("sys")
    assert inj.inject("", None) is inj.InjectOutcome.INJECTED


@pytest.mark.skipif(not inj.IS_WINDOWS, reason="只有 Windows 有 SendInput")
def test_key_input_carries_magic_signature() -> None:
    """所有注入事件必须带 MAGIC，钩子靠它区分"自己打的字"与重映射软件（G20）。"""
    inp = inj._key_input()                       # noqa: SLF001
    assert inp.ki.dwExtraInfo == inj.EXTRA_INFO_MAGIC
    assert inj.EXTRA_INFO_MAGIC == 0x56494D45     # 'VIME'，改了要和 hotkey.py 同步


@pytest.mark.skipif(not inj.IS_WINDOWS, reason="只有 Windows 有 SendInput")
def test_send_marks_every_input() -> None:
    events = inj.build_events("你好\n")          # 3 个事件 = 6 个 INPUT
    inputs: list = []
    original = inj.user32.SendInput

    def fake_send(count, arr, size):             # noqa: ANN001
        inputs.extend(arr[i].ki.dwExtraInfo for i in range(count))
        return count

    inj.user32.SendInput = fake_send
    try:
        sent = inj._send(events)                 # noqa: SLF001
    finally:
        inj.user32.SendInput = original

    assert sent == 6
    assert set(inputs) == {inj.EXTRA_INFO_MAGIC}


# ---------------------------------------------------------------- 剪贴板兜底


class FakeClipboard:
    """记录调用的假剪贴板 —— 单测绝不碰用户真实剪贴板。"""

    def __init__(self, text: str | None = "用户原来的内容") -> None:
        self._text = text
        self.formats: dict[int, bytes] = {inj.CF_UNICODETEXT: b"x"} if text else {}
        self.writes: list[str] = []
        self.restored = 0
        self.snapshot_ok = True
        self.write_ok = True

    def snapshot(self):                          # noqa: ANN201
        if not self.snapshot_ok:
            return None
        return inj.ClipboardSnapshot(formats=dict(self.formats))

    def text(self):                              # noqa: ANN201
        return self._text

    def write(self, text: str) -> bool:
        if not self.write_ok:
            return False
        self._text = text
        self.writes.append(text)
        return True

    def restore(self, snap) -> bool:             # noqa: ANN001
        self.restored += 1
        return True


def _focus(elevated: bool | None = False) -> inj.FocusSnapshot:
    return inj.FocusSnapshot(hwnd=100, pid=200, class_name="Edit", elevated=elevated, is_console=False)


@pytest.fixture()
def no_win32(monkeypatch):  # noqa: ANN001
    """把焦点/提权/文本探测固定住，测试不依赖当前前台是什么窗口。"""
    monkeypatch.setattr(inj, "snapshot_focus", lambda: _focus())
    monkeypatch.setattr(inj, "self_elevated", lambda: False)
    monkeypatch.setattr(inj, "probe_focus_text", lambda: None)


def test_definitely_unchanged_compares_text_when_available() -> None:
    before = inj.FocusText(3, "abc")
    assert inj.definitely_unchanged(before, inj.FocusText(3, "abc")) is True
    # 等长替换（粘贴覆盖选区）也算变了 —— 只比长度会误判成"没打进去"
    assert inj.definitely_unchanged(before, inj.FocusText(3, "xyz")) is False
    # 读不到全文时只比长度
    assert inj.definitely_unchanged(inj.FocusText(5000), inj.FocusText(5000)) is True
    # 探测不到 ≠ 没变化：不能兜底，否则同一句话会打两遍
    assert inj.definitely_unchanged(None, before) is False
    assert inj.definitely_unchanged(before, None) is False


def test_is_edit_class_recognizes_winforms_and_plain_edits() -> None:
    assert inj.is_edit_class("Edit")
    assert inj.is_edit_class("RichEdit50W")
    # WinForms 的类名带前缀，实测就是这个形态
    assert inj.is_edit_class("WindowsForms10.EDIT.app.0.314dfc2_r14_ad1")
    assert not inj.is_edit_class("Chrome_WidgetWin_1")
    assert not inj.is_edit_class("ConsoleWindowClass")


def test_clipboard_strategy_pastes_and_restores(no_win32, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeClipboard("用户原来的内容")
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "_send_paste", lambda: True)

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="clipboard", clipboard_restore_ms=0))

    assert res.outcome is inj.InjectOutcome.INJECTED
    assert res.strategy == "clipboard"
    assert fake.writes == ["你好"]
    assert fake.restored == 1


def test_clipboard_skips_restore_when_user_copied_meanwhile(no_win32, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeClipboard("旧内容")
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "clipboard", fake)

    def paste_and_user_copies() -> bool:
        fake._text = "用户刚复制的新内容"          # noqa: SLF001
        return True

    monkeypatch.setattr(inj, "_send_paste", paste_and_user_copies)

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="clipboard", clipboard_restore_ms=0))

    assert res.outcome is inj.InjectOutcome.INJECTED
    assert fake.restored == 0                     # 不能覆盖用户新复制的内容


def test_clipboard_fails_explicitly_when_write_blocked(no_win32, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeClipboard()
    fake.write_ok = False
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "_send_paste", lambda: True)

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="clipboard"))

    assert res.outcome is inj.InjectOutcome.FAILED
    assert res.strategy == "clipboard"


def test_clipboard_rejects_elevated_target(no_win32, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeClipboard()
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "snapshot_focus", lambda: _focus(elevated=True))

    res = inj.inject_ex("你好", None, inj.InjectConfig(strategy="clipboard"))

    assert res.outcome is inj.InjectOutcome.ELEVATED_TARGET
    assert fake.writes == []                      # 提权目标必须显式失败，不能假装粘过


def test_auto_falls_back_when_focused_edit_did_not_change(no_win32, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeClipboard()
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "_send_unicode_text", lambda text, cfg: True)
    monkeypatch.setattr(inj, "_send_paste", lambda: True)
    monkeypatch.setattr(inj, "probe_focus_text", lambda: inj.FocusText(3, "abc"))
    # 连续观察后仍然没变化 → 才允许兜底
    monkeypatch.setattr(inj, "_wait_for_change", lambda before, timeout_ms=120: inj.FocusText(3, "abc"))
    monkeypatch.setattr(inj, "_wait_paste_landed", lambda before, timeout_ms: inj.FocusText(5, "abc你好"))

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="auto", clipboard_restore_ms=0))

    assert res.outcome is inj.InjectOutcome.INJECTED
    assert res.strategy == "clipboard"
    assert fake.writes == ["你好"]


def test_wait_for_change_retries_before_declaring_failure(no_win32, monkeypatch) -> None:  # noqa: ANN001
    """SendInput 是异步的：刚发完就读可能只看到前两个字，不能立刻判定"没打进去"。"""
    snaps = iter([inj.FocusText(3, "abc"), inj.FocusText(4, "abc你")])
    monkeypatch.setattr(inj, "probe_focus_text", lambda: next(snaps))

    now = inj._wait_for_change(inj.FocusText(3, "abc"), timeout_ms=200)  # noqa: SLF001

    assert now == inj.FocusText(4, "abc你")
    assert inj.definitely_unchanged(inj.FocusText(3, "abc"), now) is False


def test_auto_keeps_unicode_when_text_grew(no_win32, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeClipboard()
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "_send_unicode_text", lambda text, cfg: True)
    snaps = iter([inj.FocusText(3, "abc"), inj.FocusText(5, "abc你好")])
    monkeypatch.setattr(inj, "probe_focus_text", lambda: next(snaps))

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="auto"))

    assert res.outcome is inj.InjectOutcome.INJECTED
    assert res.strategy == "unicode"
    assert fake.writes == []


def test_auto_does_not_fallback_when_target_cannot_be_probed(no_win32, monkeypatch) -> None:  # noqa: ANN001
    """控制台/游戏读不到文本 —— 兜底会打两遍，宁可不兜底。"""
    fake = FakeClipboard()
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "_send_unicode_text", lambda text, cfg: True)
    monkeypatch.setattr(inj, "probe_focus_text", lambda: None)

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="auto"))

    assert res.strategy == "unicode"
    assert fake.writes == []


def test_clipboard_waits_for_paste_to_land_before_restoring(no_win32, monkeypatch) -> None:  # noqa: ANN001
    """恢复剪贴板前必须确认粘贴已生效，否则粘出来的是原内容。"""
    fake = FakeClipboard()
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "_send_paste", lambda: True)
    snaps = iter([
        inj.FocusText(3, "abc"),
        inj.FocusText(3, "abc"),                  # 刚发 Ctrl+V，还没生效
        inj.FocusText(5, "abc你好"),
    ])
    monkeypatch.setattr(inj, "probe_focus_text", lambda: next(snaps))

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="clipboard", clipboard_restore_ms=50))

    assert res.outcome is inj.InjectOutcome.INJECTED
    assert fake.restored == 1


def test_clipboard_reports_failure_when_probeable_target_ignores_paste(no_win32, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeClipboard()
    monkeypatch.setattr(inj, "clipboard", fake)
    monkeypatch.setattr(inj, "_send_paste", lambda: True)
    monkeypatch.setattr(inj, "probe_focus_text", lambda: inj.FocusText(3, "abc"))

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="clipboard", clipboard_restore_ms=0))

    assert res.outcome is inj.InjectOutcome.FAILED
    assert res.strategy == "clipboard"
    assert fake.restored == 1                    # 失败了也要把剪贴板还回去


def test_unknown_strategy_fails_loudly(no_win32, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeClipboard()
    monkeypatch.setattr(inj, "clipboard", fake)

    res = inj.inject_ex("你好", _focus(), inj.InjectConfig(strategy="nope"))

    assert res.outcome is inj.InjectOutcome.FAILED
    assert res.strategy == "none"
    assert fake.writes == []


@pytest.mark.skipif(not inj.IS_WINDOWS, reason="只有 Windows 有 SendInput")
def test_paste_combo_is_ctrl_v_with_magic() -> None:
    states: list[tuple[int, int, int]] = []
    original = inj.user32.SendInput

    def fake_send(count, arr, size):             # noqa: ANN001
        states.extend((arr[i].ki.wVk, arr[i].ki.dwFlags, arr[i].ki.dwExtraInfo) for i in range(count))
        return count

    inj.user32.SendInput = fake_send
    try:
        assert inj._send_paste() is True          # noqa: SLF001
    finally:
        inj.user32.SendInput = original

    keys = [(vk, flags) for vk, flags, _ in states]
    assert keys == [
        (inj.VK_CONTROL, 0),
        (inj.VK_V, 0),
        (inj.VK_V, inj.KEYEVENTF_KEYUP),
        (inj.VK_CONTROL, inj.KEYEVENTF_KEYUP),
    ]
    assert all(extra == inj.EXTRA_INFO_MAGIC for _, _, extra in states)
