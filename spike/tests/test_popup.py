"""浮窗（`RecordingPopup`）的纯函数与配色：不需要真开窗口。"""

from __future__ import annotations

import pytest

from voiceime_spike import popup


def test_layout_is_32px_strip() -> None:
    """整体高度 32px 是需求（细长条 OSD），别被后来的改动撑高。"""
    assert popup.H == 32
    assert popup.W > popup.H * 5
    # 图标 + 动效区必须都塞在窗口里
    assert popup.PAD + popup.ICON < popup.WAVE_X < popup.W
    assert popup.WAVE_X + popup.WAVE_W <= popup.W


def test_envelope_is_symmetric_and_bounded() -> None:
    n = popup.BARS
    vals = [popup.envelope(i, n) for i in range(n)]
    assert all(0.0 < v <= 1.0 for v in vals)
    assert vals[0] == pytest.approx(vals[-1])          # 两头一样低
    assert vals[n // 2] == pytest.approx(max(vals))    # 中间最高


def test_bar_length_grows_with_volume() -> None:
    i = popup.BARS // 2
    quiet = popup.bar_length(0.0, i)
    loud = popup.bar_length(1.0, i)
    assert 0 < quiet < loud
    # 不能超出 32px 高的条：半长 ≤ 一半高度
    assert loud <= popup.WAVE_CY


def test_bar_length_clamps_out_of_range_values() -> None:
    i = popup.BARS // 2
    assert popup.bar_length(-3.0, i) == popup.bar_length(0.0, i)
    assert popup.bar_length(9.0, i) == popup.bar_length(1.0, i)


def test_blend_endpoints_and_midpoint() -> None:
    assert popup.blend("#000000", "#ffffff", 0.0) == "#000000"
    assert popup.blend("#000000", "#ffffff", 1.0) == "#ffffff"
    assert popup.blend("#000000", "#ffffff", 0.5) == "#808080"
    assert popup.blend("#000000", "#ffffff", 5.0) == "#ffffff"   # 越界夹紧


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_glass_uses_design_tokens(theme: str) -> None:
    """浮窗配色必须来自主界面主题（主题跟随），不许写死另一套 hex。"""
    from voiceime_spike.ui import DARK, LIGHT

    t = DARK if theme == "dark" else LIGHT
    g = popup._Glass(theme)
    assert g.name == t.name
    assert g.border == t.BORDER
    assert g.accent == t.WAVE
    assert g.track in (t.DISABLED_TRACK, "#e4e4e7")
    assert g.tile == "#2563eb"          # 与托盘图标同色


def test_glass_tint_alpha_is_translucent() -> None:
    """毛玻璃上色层要留出透光（alpha < 255），否则就成纯色面板了。"""
    for theme in ("dark", "light"):
        r, g, b, a = popup._Glass(theme).tint
        assert 0 < a < 255
        assert max(r, g, b) <= 255
