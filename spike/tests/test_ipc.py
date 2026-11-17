"""stdio NDJSON IPC 服务的回归（契约见 docs/03-core-concepts/ipc-contract.md）。

硬红线：stdout 只准出现协议 JSON。所以关键用例都用 capfd 抓 fd 级输出，
逐行 `json.loads` —— 只要混进一行日志/空行/traceback 就算失败。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from xifeng import ipc, webgui
from xifeng.store import Store

SPIKE_DIR = Path(__file__).resolve().parents[1]

#: 契约里的 14 个 method（ipc-contract.md「方法表」，冻结）
CONTRACT_METHODS = {
    "bootstrap",
    "stats",
    "history",
    "history_delete",
    "settings_save",
    "settings_reset",
    "theme",
    "models_use",
    "models_find",
    "autostart",
    "prune",
    "open",
    "check_update",
    "shutdown",
}

#: D16 运行时方法（ipc-contract.md「D16 运行时方法」节，Rust 壳驱动）
RUNTIME_METHODS = {"asr_begin", "asr_feed", "asr_end", "asr_cancel", "correct", "session_record"}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "ipc.db")
    yield s
    s.close()


def _run(store: Store, *messages: object) -> list[dict]:
    """喂一串请求（dict 或原始字符串），返回解析后的全部输出行。"""
    lines = "".join(
        (m if isinstance(m, str) else json.dumps(m, ensure_ascii=False)) + "\n"
        for m in messages
    )
    out = io.StringIO()
    rc = ipc.serve(store, stdin=io.StringIO(lines), stdout=out)
    assert rc == 0
    return [json.loads(l) for l in out.getvalue().splitlines()]


def _assert_all_json(text: str) -> list[dict]:
    """stdout 的每一行都必须是合法 JSON，且不能有空行。"""
    parsed = []
    for lineno, line in enumerate(text.splitlines(), 1):
        assert line.strip(), f"stdout 第 {lineno} 行是空行"
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - 失败即红线
            raise AssertionError(f"stdout 第 {lineno} 行不是 JSON：{line!r}（{exc}）") from exc
    return parsed


# ---------------------------------------------------------------- 正常请求/响应


def test_ready_is_first_message_with_pid(store: Store) -> None:
    msgs = _run(store)
    assert len(msgs) == 1
    assert msgs[0]["event"] == "ready"
    assert isinstance(msgs[0]["result"]["pid"], int)
    assert "id" not in msgs[0]


def test_bootstrap_roundtrip(store: Store) -> None:
    ready, resp = _run(store, {"id": 1, "method": "bootstrap", "params": {}})
    assert ready["event"] == "ready"
    assert resp["id"] == 1 and resp["ok"] is True
    result = resp["result"]
    assert {"version", "dataDir", "settings", "models", "correction", "autostart"} <= set(result)
    assert "model_correct" in result["settings"]
    json.dumps(result)  # 契约要求可序列化


def test_stats_period_and_default(store: Store) -> None:
    _ready, a, b = _run(
        store,
        {"id": 1, "method": "stats", "params": {"period": "7d"}},
        {"id": 2, "method": "stats"},
    )
    for resp in (a, b):
        assert resp["ok"] is True
        assert {"kpis", "daily", "rows", "runtime", "model"} <= set(resp["result"])


def test_settings_save_persists_and_returns_full_settings(store: Store) -> None:
    _ready, saved, loaded = _run(
        store,
        {"id": 1, "method": "settings_save", "params": {"theme": "light", "threads": 4}},
        {"id": 2, "method": "bootstrap"},
    )
    assert saved["ok"] is True
    assert saved["result"]["theme"] == "light"
    assert saved["result"]["threads"] == 4
    assert loaded["result"]["settings"]["theme"] == "light"   # 真落了库


def test_history_and_delete(store: Store) -> None:
    from xifeng.trace import Outcome, Trace

    store.add_history(Trace(outcome=Outcome.INJECTED, e2e_ms=300, ms_asr=250), "你好")
    _ready, page, deleted, after = _run(
        store,
        {"id": 1, "method": "history", "params": {"period": "all", "offset": 0, "limit": 10}},
        {"id": 2, "method": "history_delete", "params": {"id": 1}},
        {"id": 3, "method": "history", "params": {"period": "all"}},
    )
    assert page["result"]["rows"][0]["text"] == "你好"
    assert deleted["result"] == {"ok": True}
    assert after["result"]["rows"] == []


def test_period_aliases_and_numeric_strings(store: Store) -> None:
    """契约表写 `"period": "7d"`、HTTP 写 `week`；两种都得能用，offset/limit 允数字串。"""
    _ready, week, today, page = _run(
        store,
        {"id": 1, "method": "stats", "params": {"period": "7d"}},
        {"id": 2, "method": "stats", "params": {"period": "1d"}},
        {"id": 3, "method": "history", "params": {"period": "all", "offset": "0", "limit": "5"}},
    )
    for resp in (week, today, page):
        assert resp["ok"] is True


def test_non_numeric_offset_is_error(store: Store) -> None:
    _ready, resp = _run(
        store, {"id": 1, "method": "history", "params": {"offset": "abc"}}
    )
    assert resp["ok"] is False and "整数" in resp["error"]


# ---------------------------------------------------------------- 错误路径不崩


def test_unknown_method_returns_error_and_keeps_going(store: Store) -> None:
    _ready, bad, good = _run(
        store,
        {"id": 1, "method": "nope", "params": {}},
        {"id": 2, "method": "prune"},
    )
    assert bad == {"id": 1, "ok": False, "error": "unknown method: nope"}
    assert good["ok"] is True          # 下一条照常处理


def test_bad_params_return_error_not_crash(store: Store) -> None:
    _ready, missing, unknown_key, wrong_type, still_alive = _run(
        store,
        {"id": 1, "method": "history_delete", "params": {}},          # 缺 id
        {"id": 2, "method": "stats", "params": {"nope": 1}},          # 多余参数
        {"id": 3, "method": "stats", "params": ["7d"]},               # params 不是对象
        {"id": 4, "method": "bootstrap"},
    )
    for resp in (missing, unknown_key, wrong_type):
        assert resp["ok"] is False and isinstance(resp["error"], str) and resp["error"]
    assert missing["error"].startswith("缺少参数")
    assert still_alive["ok"] is True


def test_missing_method_field(store: Store) -> None:
    _ready, no_method, not_object = _run(store, {"id": 1}, "[1,2,3]")
    assert no_method == {"id": 1, "ok": False, "error": "缺少 method"}
    assert not_object["ok"] is False


def test_invalid_json_line_returns_error_and_continues(store: Store) -> None:
    _ready, bad, good = _run(store, "not json", {"id": 2, "method": "prune"})
    assert bad["id"] is None and bad["ok"] is False
    assert bad["error"].startswith("invalid json")
    assert good["ok"] is True


def test_domain_error_is_reported(store: Store, tmp_path: Path) -> None:
    """Api 自己抛的 ValueError（模型目录不完整）要变成 error，不是直接崩。"""
    empty = tmp_path / "empty-model"
    empty.mkdir()
    _ready, resp = _run(
        store, {"id": 1, "method": "models_use", "params": {"path": str(empty)}}
    )
    assert resp["ok"] is False
    assert "model.onnx" in resp["error"]


# ---------------------------------------------------------------- 生命周期


def test_shutdown_replies_then_stops(store: Store) -> None:
    out = io.StringIO()
    stdin = io.StringIO(
        '{"id": 1, "method": "shutdown"}\n{"id": 2, "method": "prune"}\n'
    )
    assert ipc.serve(store, stdin=stdin, stdout=out) == 0
    msgs = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [m.get("event") for m in msgs] == ["ready", None]  # shutdown 后的请求不再处理
    assert msgs[-1] == {"id": 1, "ok": True, "result": {"ok": True}}


def test_eof_exits_gracefully(store: Store) -> None:
    out = io.StringIO()
    assert ipc.serve(store, stdin=io.StringIO(""), stdout=out) == 0
    assert [json.loads(l).get("event") for l in out.getvalue().splitlines()] == ["ready"]


def test_closed_stdout_does_not_raise(store: Store) -> None:
    """壳进程先死：Windows 上 write/flush 抛的是 OSError(EINVAL)，不是 BrokenPipeError。"""
    class DeadStream:
        def __init__(self) -> None:
            self.writes = 0

        def write(self, _text: str) -> int:
            self.writes += 1
            return 0

        def flush(self) -> None:
            raise OSError(22, "Invalid argument")     # EINVAL

    assert ipc.serve(store, stdin=io.StringIO('{"id": 1, "method": "prune"}\n'),
                     stdout=DeadStream()) == 0


def test_blank_lines_are_skipped_not_answered(store: Store) -> None:
    out = io.StringIO()
    ipc.serve(
        store,
        stdin=io.StringIO('\n\n{"id": 1, "method": "prune"}\n\n'),
        stdout=out,
    )
    msgs = [json.loads(l) for l in out.getvalue().splitlines()]
    assert len(msgs) == 2  # ready + 一条响应，空行不产出消息


# ---------------------------------------------------------------- stdout 红线（capfd）


def test_stdout_contains_only_json_lines(
    store: Store, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """混入未知 method / 非法 JSON / 参数错误（这些都会打日志）后，stdout 仍全是 JSON。"""
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "\n".join(
                [
                    '{"id": 1, "method": "bootstrap", "params": {}}',
                    '{"id": 2, "method": "nope"}',
                    "not json at all",
                    '{"id": 3, "method": "history_delete", "params": {}}',
                    '{"id": 4, "method": "settings_save", "params": {"theme": "light"}}',
                    '{"id": 5, "method": "shutdown"}',
                ]
            )
            + "\n"
        ),
    )
    # stdout 走真实 fd，交给 capfd 抓
    assert ipc.serve(store) == 0

    captured = capfd.readouterr()
    msgs = _assert_all_json(captured.out)
    assert msgs[0]["event"] == "ready"
    assert [m.get("id") for m in msgs[1:]] == [1, 2, None, 3, 4, 5]
    # 日志（含 traceback）只在 stderr
    assert "[ipc]" in captured.err


def test_serve_never_writes_non_json_even_when_api_prints(
    store: Store,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最坏情况：Api 内部 print 到 stdout。

    `run_spike --ipc` 会把 `sys.stdout` 指向 stderr 再调 serve，这里复现同一层防护，
    证明约定生效后 stdout 依旧只有协议行。
    """
    def noisy_bootstrap(self) -> dict:  # noqa: ANN001
        print("[db] ⚠ 这是一条普通日志")
        return {"version": "x"}

    monkeypatch.setattr(webgui.Api, "bootstrap", noisy_bootstrap)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id": 1, "method": "bootstrap"}\n'))

    real = sys.stdout
    sys.stdout = sys.stderr          # 与 run_spike.ipc_mode 同一层防护
    try:
        assert ipc.serve(store, stdout=real) == 0
    finally:
        sys.stdout = real

    captured = capfd.readouterr()
    assert _assert_all_json(captured.out)[0]["event"] == "ready"
    assert "这是一条普通日志" in captured.err


# ---------------------------------------------------------------- 方法表一致性


def test_method_table_matches_contract_and_api() -> None:
    assert set(ipc.METHODS) == CONTRACT_METHODS
    assert len(ipc.METHODS) == 14
    for method, target in ipc.METHODS.items():
        if method == "shutdown":
            assert target is None          # 主循环自己处理，没有 Api 方法
            continue
        assert callable(getattr(webgui.Api, target, None)), f"{method} → {target} 不存在"


def test_runtime_method_table_matches_contract() -> None:
    """D16 运行时方法与契约一致，且不与 HTTP 对应方法重叠。"""
    from xifeng.asr_runtime import AsrRuntime

    assert set(ipc.RUNTIME_METHODS) == RUNTIME_METHODS
    assert len(ipc.RUNTIME_METHODS) == 6
    assert set(ipc.RUNTIME_METHODS) & set(ipc.METHODS) == set()
    for method, target in ipc.RUNTIME_METHODS.items():
        assert callable(getattr(AsrRuntime, target, None)), f"{method} → {target} 不存在"


def test_http_routes_are_all_exposed_over_ipc() -> None:
    """HTTP 模式下能用的端点，IPC 侧 MUST 都有（否则换传输层会少功能）。"""
    import inspect

    http_methods = {
        name
        for name, _ in inspect.getmembers(webgui.Api, predicate=inspect.isfunction)
        if not name.startswith("_")
    } - {"ping"}          # ping 只服务 Web 心跳看门狗，IPC 没有窗口需要续命
    assert http_methods <= set(ipc.METHODS)


# ---------------------------------------------------------------- 真实进程


def test_run_spike_ipc_subprocess(tmp_path: Path) -> None:
    """端到端：`run_spike.py --ipc` 拉起来，协议正常、退出码 0、stdout 干净。

    用 `sys.executable`（uv 管的 .venv 解释器），不碰系统 Python。
    """
    payload = "\n".join(
        [
            '{"id": 1, "method": "bootstrap", "params": {}}',
            '{"id": 2, "method": "bogus"}',
            '{"id": 3, "method": "shutdown"}',
        ]
    ) + "\n"
    proc = subprocess.run(
        [sys.executable, "run_spike.py", "--ipc", "--db", str(tmp_path / "cli.db")],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(SPIKE_DIR),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    msgs = _assert_all_json(proc.stdout)
    assert msgs[0]["event"] == "ready"
    assert [m.get("id") for m in msgs[1:]] == [1, 2, 3]
    assert msgs[2] == {"id": 2, "ok": False, "error": "unknown method: bogus"}
    assert msgs[3]["result"] == {"ok": True}
    # 启动横幅之类的日志只能出现在 stderr
    assert "bootstrap" not in proc.stdout


def test_ipc_flag_parsed_by_cli() -> None:
    import run_spike

    assert run_spike.parse_args(["--ipc"]).ipc is True
    assert run_spike.parse_args([]).ipc is False
