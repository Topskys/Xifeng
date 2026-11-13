"""托盘 / 自启 / 无限 PTT 的连线测试。

不碰真注册表、不装钩子、不开麦克风：注册表用假 winreg，按键/录音全打桩。
真正需要人工看的是托盘图标与窗口外观，那部分只能手测。
"""

from __future__ import annotations

import io
import sys

import run_spike
from voiceime_spike import applog, autostart, hotkey


class _Key:
    def __init__(self, data: dict[str, str]) -> None:
        self.data = data

    def __enter__(self) -> "_Key":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeWinreg:
    """只实现 autostart 用到的那几个函数，并且行为与真 winreg 一致。"""

    HKEY_CURRENT_USER = 0
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    def CreateKey(self, root: object, path: str) -> _Key:  # noqa: N802
        return _Key(self.keys.setdefault(path, {}))

    def OpenKey(self, root: object, path: str, reserved: int = 0, access: int = 0) -> _Key:  # noqa: N802
        if path not in self.keys:
            raise FileNotFoundError(path)
        return _Key(self.keys[path])

    def QueryValueEx(self, key: _Key, name: str):  # noqa: N802, ANN201
        if name not in key.data:
            raise FileNotFoundError(name)
        return key.data[name], self.REG_SZ

    def SetValueEx(self, key: _Key, name: str, reserved: int, typ: int, value: str) -> None:  # noqa: N802
        key.data[name] = value

    def DeleteValue(self, key: _Key, name: str) -> None:  # noqa: N802
        if name not in key.data:
            raise FileNotFoundError(name)
        del key.data[name]


def test_autostart_cycle(monkeypatch) -> None:  # noqa: ANN001
    fake = _FakeWinreg()
    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "winreg", fake)

    assert autostart.is_enabled() is False
    assert "关闭" in autostart.status_text()

    cmd = autostart.enable()
    assert "--tray" in cmd
    assert autostart.is_enabled() is True
    assert "开启" in autostart.status_text()
    assert cmd in autostart.status_text()

    assert autostart.toggle() is False
    assert autostart.is_enabled() is False
    assert autostart.disable() is False       # 已经没了


def test_ptt_loop_infinite_stops_after_one_wait(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []
    monkeypatch.setattr(hotkey, "IS_WINDOWS", True)
    monkeypatch.setattr(hotkey, "ensure_hook", lambda: True)
    monkeypatch.setattr(hotkey, "input_backend", lambda: "hook")
    monkeypatch.setattr(hotkey, "start_mic", lambda sample_rate=16000: True)
    monkeypatch.setattr(hotkey, "stop_mic", lambda: calls.append("stop"))

    def fake_record(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        assert callable(kwargs.get("should_stop"))
        calls.append("record")
        result = hotkey.PttResult()
        result.stopped = True
        return result

    monkeypatch.setattr(hotkey, "record_ptt", fake_record)
    args = run_spike.parse_args(["--ptt", "--times", "0", "--no-inject", "--no-db"])
    assert run_spike.ptt_loop(object(), args, should_stop=lambda: False) == 0
    assert calls == ["record", "stop"]


def test_ptt_loop_stops_before_first_wait(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []
    monkeypatch.setattr(hotkey, "IS_WINDOWS", True)
    monkeypatch.setattr(hotkey, "ensure_hook", lambda: True)
    monkeypatch.setattr(hotkey, "input_backend", lambda: "hook")
    monkeypatch.setattr(hotkey, "start_mic", lambda sample_rate=16000: True)
    monkeypatch.setattr(hotkey, "stop_mic", lambda: calls.append("stop"))
    monkeypatch.setattr(hotkey, "record_ptt", lambda *a, **k: calls.append("record"))

    args = run_spike.parse_args(["--ptt", "--times", "0", "--no-inject", "--no-db"])
    # 托盘在启动瞬间就点了「退出」：一次都不该进录音
    assert run_spike.ptt_loop(object(), args, should_stop=lambda: True) == 0
    assert calls == ["stop"]


def test_wait_for_press_honors_stop() -> None:
    """等按键时必须能被退出信号打断，否则托盘「退出」会卡住。"""
    assert hotkey.wait_for_press("ctrl", check=lambda: False, stop=lambda: True) is False


def test_tray_quit_stops_icon_and_signals() -> None:
    from voiceime_spike import tray as tray_mod

    quit_called: list[bool] = []
    stopped: list[bool] = []

    class FakeIcon:
        def stop(self) -> None:
            stopped.append(True)

    tray = tray_mod.Tray(on_quit=lambda: quit_called.append(True))
    tray._icon = FakeIcon()       # type: ignore[assignment]
    tray._quit()

    assert stopped == [True]
    assert quit_called == [True]
    assert tray._icon is None


def test_tray_icon_image_is_self_contained() -> None:
    """图标必须由代码画出来 —— 打包时就不用带图片资源，也不会找不到文件。"""
    from voiceime_spike.tray import _image

    img = _image()
    assert img.size == (64, 64)
    assert img.mode == "RGBA"


def test_tray_single_instance_mutex() -> None:
    """两个托盘进程会各装一个钩子、各录一份音；第二个必须被挡掉。"""
    import os
    import time

    from voiceime_spike import tray as tray_mod

    name = f"Local\\VoiceIME-test-{os.getpid()}-{time.time_ns()}"
    assert tray_mod.acquire_single_instance(name) is True
    assert tray_mod.acquire_single_instance(name) is False


def test_web_dispatch_passes_tab(monkeypatch) -> None:  # noqa: ANN001
    """托盘/命令行打开主窗口走 Web GUI，必须把页签透传过去。"""
    from voiceime_spike import webgui

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        webgui, "run", lambda db_path=None, tab="stats": seen.update(db=db_path, tab=tab) or 0
    )
    assert run_spike.main(["--web", "--tab", "config"]) == 0
    assert seen == {"db": None, "tab": "config"}


def test_tray_gui_command(monkeypatch, tmp_path) -> None:
    """托盘「显示主界面」优先开 Tauri 壳；找不到壳才退回 `--web --tab` 兜底。"""
    from voiceime_spike import tray as tray_mod

    # 源码形态：没有壳 exe → Web GUI 兜底
    cmd = tray_mod.gui_command("about")
    assert cmd[-3:] == ["--web", "--tab", "about"]
    assert "--gui" not in cmd

    # 冻结 + 壳存在 → 开壳（sidecar 上两级是安装根）
    fake_core = tmp_path / "core" / "VoiceIME-Core" / "VoiceIME-Core.exe"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_bytes(b"")
    shell = tmp_path / "voiceime-tauri.exe"
    shell.write_bytes(b"")
    monkeypatch.setattr(tray_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(tray_mod.sys, "executable", str(fake_core))
    assert tray_mod.gui_command() == [str(shell)]

    # 冻结但壳缺失（PyInstaller 单体形态）→ 退回 Web GUI
    shell.unlink()
    frozen = tray_mod.gui_command()
    assert frozen == [str(fake_core), "--web", "--tab", "stats"]


def test_applog_rescues_missing_stdout(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """冻结的 windowed exe 里 stdout 是 None，一句 print 就会崩；日志必须兜住。"""
    monkeypatch.setenv("IME_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    handle = applog.setup()
    assert handle is not None
    print("⚠ 警告也能写")           # 不能抛 AttributeError
    print("第二行", file=sys.stderr)
    applog.restore(handle, io.StringIO(), io.StringIO())

    text = (tmp_path / applog.LOG_NAME).read_text(encoding="utf-8")
    assert "⚠ 警告也能写" in text
    assert "第二行" in text


def test_applog_force_file_keeps_console_streams(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("IME_DATA_DIR", str(tmp_path))
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    handle = applog.setup(force_file=True)   # 托盘：控制台要隐藏，必须落文件
    assert handle is not None
    print("托盘日志")
    applog.restore(handle, out, err)

    assert (tmp_path / applog.LOG_NAME).read_text(encoding="utf-8").strip() == "托盘日志"
    assert sys.stdout is out and sys.stderr is err
