"""stdio NDJSON IPC 服务：Tauri 壳拉起 Python 核心时用的传输层。

协议见 `docs/03-core-concepts/ipc-contract.md`（冻结）。要点：

- stdin 逐行读 JSON 请求，stdout 逐行写 JSON 响应（一行一条，UTF-8，无 BOM）
- **stdout 是协议专用通道**：本模块任何日志都 MUST 走 stderr；
  调用方（`run_spike --ipc`）在进入 `serve` 前也要把 `sys.stdout` 换到 stderr
- 未知 method / 参数错误 / 单条消息异常一律回 `ok: false`，不崩进程
- 只有 `shutdown` 或 stdin EOF 才退出

业务逻辑不在这里重写：方法名与参数语义和 HTTP 模式一一对应，
直接分发到 `webgui.Api`，前端换传输层即可，业务代码零改动。
"""

from __future__ import annotations

import errno
import inspect
import json
import os
import sys
import traceback
from collections.abc import Callable
from typing import IO, Any

from . import settings as user_settings
from .asr_runtime import AsrRuntime
from .store import Store
from .webgui import Api

#: method → `Api` 上的方法名（`shutdown` 没有对应方法，由主循环自己处理）
METHODS: dict[str, str | None] = {
    "bootstrap": "bootstrap",
    "stats": "stats",
    "history": "history",
    "history_delete": "history_delete",
    "settings_save": "settings_save",
    "settings_reset": "settings_reset",
    "theme": "theme",
    "models_use": "models_use",
    "models_find": "models_find",
    "autostart": "autostart",
    "prune": "prune",
    "open": "open",
    "check_update": "check_update",
    "shutdown": None,
}

#: D16 运行时方法（Rust 壳驱动的推理通道）：不在 `Api` 上，
#: 由 `asr_runtime.AsrRuntime` 实现，见 ipc-contract.md「方法表」
RUNTIME_METHODS: dict[str, str] = {
    "asr_begin": "begin",
    "asr_feed": "feed",
    "asr_end": "end",
    "asr_cancel": "cancel",
    "correct": "correct",
    "session_record": "session_record",
    "set_language": "set_language",
}

#: 参数是一整个 JSON 对象的方法（`Api` 里签名就是 `body: dict`）
_BODY_PARAM = frozenset(
    {
        "history_delete",
        "settings_save",
        "theme",
        "models_use",
        "models_find",
        "autostart",
        "open",
    }
)

#: 不接参数的方法
_NO_PARAM = frozenset({"bootstrap", "settings_reset", "prune", "check_update"})

#: 契约表里 `stats`/`history` 的 period 写的是 `"7d"` 这种形式，HTTP 侧用的是
#: `today|week|all`。两种写法都收：前端换传输层后不会因为拼写差异拿到错数据。
_PERIOD_ALIASES = {"1d": "today", "7d": "week", "30d": "all", "90d": "all", "all": "all"}


def _period(value: object) -> object:
    return _PERIOD_ALIASES.get(value, value) if isinstance(value, str) else value


def _as_int(value: object, method: str, key: str) -> int:
    """JSON 里 offset/limit 可能被写成 `"10"`；类型错了要显式报错，不靠 int() 猜。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{method}.{key} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{method}.{key} 必须是整数") from exc


MAX_ERROR_CHARS = 200

#: 对端关掉 stdout 后，write/flush 可能抛的 errno（Windows 是 EINVAL/EPIPE）
_PIPE_GONE = frozenset({errno.EPIPE, errno.EINVAL, errno.EBADF})


def _log(msg: str) -> None:
    """日志只走 stderr —— 写进 stdout 会把协议整条冲掉。"""
    try:
        print(f"[ipc] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - 连 stderr 都不可写时也不能影响协议
        pass


class _Protocol:
    """stdout 行写入器：保证「一行一个合法 JSON」且不夹带换行翻译。

    Windows 上文本模式默认把 `\\n` 翻成 `\\r\\n`，协议要的是纯 LF；
    改不动编码时退回 ASCII 转义（`\\uXXXX` 在任何编码下都能安全写出，
    对端 `JSON.parse` 解析结果完全一样）。
    """

    def __init__(self, stream: IO[str]) -> None:
        self.stream = stream
        self.ascii_only = True
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")  # type: ignore[attr-defined]
            self.ascii_only = False
        except Exception:  # noqa: BLE001 - StringIO / 自定义流没有 reconfigure
            self.ascii_only = True

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=self.ascii_only, separators=(",", ":"))
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except (BrokenPipeError, OSError) as exc:
            # Windows 关掉的管道抛的是 OSError(EINVAL)，不是 BrokenPipeError
            if isinstance(exc, BrokenPipeError) or getattr(exc, "errno", None) in _PIPE_GONE:
                raise BrokenPipeError("stdout 已关闭") from exc
            raise


def _as_dict(params: object, method: str) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError(f"{method} 的 params 必须是 JSON 对象")
    return params


def _keyword_args(fn: Callable[..., Any], method: str, params: object) -> dict[str, Any]:
    """按 `Api` 方法的具名参数过滤，多出来的键显式报错（不静默吞掉）。"""
    body = _as_dict(params, method)
    allowed = set(inspect.signature(fn).parameters)
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValueError(f"{method} 不接受参数: {', '.join(unknown)}")
    return body


def _dispatch(api: Api, runtime: AsrRuntime, method: str, params: object) -> Any:
    rt_target = RUNTIME_METHODS.get(method)
    if rt_target is not None:
        fn = getattr(runtime, rt_target)
        args = _keyword_args(fn, method, params)
        return fn(**args)
    target = METHODS.get(method)
    if target is None:
        raise KeyError(f"unknown method: {method}")
    fn = getattr(api, target)
    if method in _NO_PARAM:
        if _as_dict(params, method):
            raise ValueError(f"{method} 不接受参数")
        result = fn()
        if method == "settings_reset":
            _sync_runtime(api, runtime)
        return result
    if method in _BODY_PARAM:
        result = fn(_as_dict(params, method))
        if method in ("settings_save", "models_use"):
            _sync_runtime(api, runtime)
        return result
    args = _keyword_args(fn, method, params)
    for key in ("period", "offset", "limit", "id"):
        if key in args:
            args[key] = _period(args[key]) if key == "period" else _as_int(args[key], method, key)
    return fn(**args)


def _sync_runtime(api: Api, runtime: AsrRuntime) -> None:
    """设置保存后把用户设置同步给运行时（P2-② 热更）。

    分两类，口径要如实：

    - **构造参数**（语言、推理线程数、模型目录、ITN）：sherpa 的 recognizer 在
      构造时就把它们吃掉了，改了必须重建（数秒）。这里在同一次 IPC 调用里
      做完，用户点「保存」就生效。
    - **非构造参数**（录音段切分、首尾静音裁剪、模型纠错）：只是每次解码
      时读的开关，改属性即可，下一次说话生效。

    失败（模型缺失、非法语言）不抛给前端：设置已经存进 JSON 了，
    运行时保持新配置、下次加载时重试——抛错会让前端误以为"保存失败"。
    """
    s = api.settings
    try:
        runtime.apply_settings(
            language=getattr(s, "language", None),
            threads=getattr(s, "threads", None),
            model_dir=(getattr(s, "model_dir", "") or None),
            use_itn=getattr(s, "use_itn", None),
            use_vad=getattr(s, "use_vad", None),
            trim_silence=getattr(s, "trim_silence", None),
            model_correct=getattr(s, "model_correct", None),
        )
    except Exception as exc:  # noqa: BLE001 - 热更失败不该让保存失败
        _log(f"⚠ 设置热更失败（已保存，下次启动生效）：{type(exc).__name__}: {exc}")


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, KeyError):
        # KeyError 的 str() 自带引号，直接给前端不好读
        missing = exc.args[0] if exc.args else ""
        text = f"缺少参数: {missing}"
    else:
        text = str(exc) or type(exc).__name__
    return text[:MAX_ERROR_CHARS]


def _is_expected(exc: BaseException) -> bool:
    """参数类错误（缺参数/类型不对）是正常业务流，不必打整段栈。"""
    return isinstance(exc, (KeyError, ValueError, TypeError))


def _handle(api: Api, runtime: AsrRuntime, message: object) -> tuple[dict[str, Any], bool]:
    """处理一条消息，返回 (响应, 是否该退出)。任何异常都变成 error 响应。"""
    if not isinstance(message, dict):
        return {"id": None, "ok": False, "error": "消息必须是 JSON 对象"}, False

    req_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str) or not method.strip():
        return {"id": req_id, "ok": False, "error": "缺少 method"}, False
    method = method.strip()

    if method == "shutdown":
        return {"id": req_id, "ok": True, "result": {"ok": True}}, True

    if method not in METHODS and method not in RUNTIME_METHODS:
        return {"id": req_id, "ok": False, "error": f"unknown method: {method}"}, False

    try:
        result = _dispatch(api, runtime, method, message.get("params"))
    except Exception as exc:  # noqa: BLE001 - 单条消息失败不能带走进程
        _log(f"{method} 失败：{type(exc).__name__}: {exc}")
        if not _is_expected(exc):
            _log(traceback.format_exc().rstrip())
        return {"id": req_id, "ok": False, "error": _error_text(exc)}, False
    return {"id": req_id, "ok": True, "result": result}, False


def serve(
    store: Store,
    settings: user_settings.Settings | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    runtime: AsrRuntime | None = None,
) -> int:
    """跑 IPC 主循环，直到 `shutdown` 或 stdin EOF。

    `stdout` 必须是「真正的」协议流（`run_spike --ipc` 会把 `sys.stdout`
    换到 stderr 后把原始流传进来），这里不再 print 任何非协议内容。

    `runtime` 是 D16 推理运行时（`asr_begin/feed/end/cancel` 四个方法的状态）；
    不传时用默认配置兜底（懒加载、不起后台 warmup 线程——测试路径不与全局
    ASR 状态竞争）。`run_spike.ipc_mode` 会按命令行参数显式构造并传入，
    ready 之后由后台线程 warmup，首次 `asr_begin` 不等冷启动。
    """
    api = Api(store)
    if settings is not None:
        # 命令行/数据库已经算过一次，别让 Api 再读一遍（也保证 --ipc 与其他模式一致）
        api.settings = settings
    if runtime is None:
        runtime = AsrRuntime(autostart_warmup=False)

    out = _Protocol(stdout if stdout is not None else sys.stdout)
    src = stdin if stdin is not None else sys.stdin
    if stdin is None:
        # 管道下 Windows 默认按区域编码（gbk）解，契约要求 UTF-8；
        # utf-8-sig 顺带吃掉可能存在的 BOM（PowerShell 管道/旧工具会加）
        try:
            src.reconfigure(encoding="utf-8-sig", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    try:
        out.send({"event": "ready", "result": {"pid": os.getpid()}})
        warmup_thread = runtime.start_warmup() if runtime.autostart_warmup else None
        try:
            # ready 已发出才起后台 warmup：绝不阻塞 ready，也绝不阻塞协议主循环
            return _loop(api, runtime, src, out)
        finally:
            if warmup_thread is not None:
                # shutdown/EOF 后 join warmup 线程再退出，保证退出码干净
                runtime.stop_warmup()
    except BrokenPipeError:
        # 壳进程先退了：协议通道没人读，收工（不留僵尸进程）
        _log("stdout 已关闭（壳进程退出），本地收工")
        return 0


def _loop(api: Api, runtime: AsrRuntime, src: IO[str], out: _Protocol) -> int:
    while True:
        line = src.readline()
        if not line:      # EOF：壳关掉了 stdin
            _log("stdin EOF，退出")
            return 0
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _log(f"非法 JSON：{exc}")
            out.send({"id": None, "ok": False, "error": f"invalid json: {exc}"})
            continue

        response, stop = _handle(api, runtime, message)
        out.send(response)
        if stop:
            _log("shutdown，退出")
            return 0
