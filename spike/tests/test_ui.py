"""ui 控件库的关键回归：主题切换与占位符边界。

为什么测这两个：set_theme 改的是模块全局令牌，任何"import 时定格"的默认参数
都会让浅色主题下出现深色残块；占位符写入 textvariable 则会把占位文本当成
用户输入（历史页搜索、术语表添加都踩过）。
"""

import pytest

tk = pytest.importorskip("tkinter")

from xifeng import ui


@pytest.fixture(scope="module")
def root():
    # 一个进程只能初始化一次 Tcl；destroy 后再 Tk() 会报
    # `invalid command name "tcl_findLibrary"`，所以整个模块共用一个 root
    r = tk.Tk()
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture(autouse=True)
def dark_theme():
    """每个用例结束恢复 dark，避免污染其它测试的主题状态。"""
    yield
    ui.set_theme("dark")


def test_set_theme_switches_tokens():
    ui.set_theme("light")
    assert ui.current_theme() == "light"
    assert ui.BG == ui.LIGHT.BG
    assert ui.PRIMARY == ui.LIGHT.PRIMARY
    assert ui.CHART == ui.LIGHT.CHART
    ui.set_theme("dark")
    assert ui.current_theme() == "dark"
    assert ui.BG == ui.DARK.BG


def test_unknown_theme_falls_back_to_dark():
    ui.set_theme("nonsense")
    assert ui.current_theme() == "dark"


def test_label_defaults_follow_theme(root):
    # 默认参数必须在调用期解析：import 时定格的话，浅色下会画出深色残块
    ui.set_theme("light")
    lbl = ui.label(root, "hi")
    assert lbl.cget("bg") == ui.LIGHT.CARD
    assert lbl.cget("fg") == ui.LIGHT.PRIMARY


def test_input_placeholder_not_written_to_var(root):
    var = tk.StringVar()
    inp = ui.Input(root, placeholder="搜索文本…", textvariable=var)
    assert var.get() == ""          # 占位符绝不进 var（G：会被当用户输入）
    assert inp.get() == ""
    inp.set("abc")
    assert var.get() == "abc"
    assert inp.get() == "abc"
    var.set("")                     # 外部清空也要能回到占位状态
    assert inp.get() == ""
