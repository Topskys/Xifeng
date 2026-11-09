"""webgui.Api 的纯逻辑回归（不起窗口、不开 HTTP）。

约束来源：API 返回值必须 JSON 可序列化；settings 保存走 user_settings 校验；
models_use 对不完整目录必须显式报错（禁止静默）。
"""

from __future__ import annotations

import pytest

from voiceime_spike import webgui
from voiceime_spike.store import Store


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / "t.db")
    yield webgui.Api(store)
    store.close()


def test_bootstrap_shape(api):
    b = api.bootstrap()
    assert set(b) >= {"version", "dataDir", "settings", "models", "correction", "autostart"}
    assert isinstance(b["correction"]["available"], bool)
    assert b["correction"]["dir"]  # 缺模型时配置页要告诉用户放哪
    assert "model_correct" in b["settings"]
    json.dumps(b)  # JSON 可序列化


import json  # noqa: E402


def test_settings_roundtrip_and_theme(api):
    s = api.settings_save({"clipboard_restore_ms": 800, "theme": "light", "model_correct": True})
    assert s["clipboard_restore_ms"] == 800
    assert s["theme"] == "light"
    assert s["model_correct"] is True
    # 非法值回退默认（settings.update 校验）
    s = api.settings_save({"theme": "nonsense", "threads": 999})
    assert s["theme"] == "dark"
    assert s["threads"] == api.settings.threads
    # theme 端点等价于 settings_save({"theme": ...})
    assert api.theme({"theme": "light"})["theme"] == "light"


def test_model_correct_defaults_off(api):
    assert api.bootstrap()["settings"]["model_correct"] is False


def test_models_use_rejects_incomplete_dir(api, tmp_path):
    empty = tmp_path / "empty-model"
    empty.mkdir()
    with pytest.raises(ValueError):
        api.models_use({"path": str(empty)})


def test_history_endpoints(api):
    from voiceime_spike.trace import Outcome, Trace

    t = Trace(outcome=Outcome.INJECTED, e2e_ms=300, ms_asr=250, fixes=1).start()
    api.store.add_history(t, "你好", raw_text="你好", clean_text="你好")
    h = api.history("all", 0, 50)
    assert h["rows"][0]["text"] == "你好" and h["has_more"] is False
    api.history_delete({"id": h["rows"][0]["id"]})
    assert api.history("all", 0, 50)["rows"] == []


def test_ping_keeps_service_alive(api):
    """前端每 5s 心跳；服务端没有心跳超过 30s 才退出（G31C）。"""
    assert api.last_ping is None
    assert api.ping({}) == {"ok": True}
    assert api.last_ping is not None


def test_service_url_maps_unknown_tab_to_stats():
    assert webgui._service_url("config").endswith("#config")
    assert webgui._service_url("about").endswith("#about")
    assert webgui._service_url("nonsense").endswith("#stats")


def test_watchdog_shuts_down_after_no_heartbeat():
    import time as _time

    class FakeServer:
        def __init__(self) -> None:
            self.down = False

        def shutdown(self) -> None:
            self.down = True

    class FakeApi:
        last_ping = _time.monotonic() - 31

    server = FakeServer()
    webgui._watchdog(server, FakeApi(), interval=0.01, timeout=30)
    assert server.down
