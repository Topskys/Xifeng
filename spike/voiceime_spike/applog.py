"""应用日志：把 stdout/stderr 变成「永远能写」的 UTF-8 流。

为什么需要单独一层：同一个 `print(f"[db] ⚠ ...")` 在两种形态下会直接崩掉整个进程——

1. **冻结的 windowed exe**：`sys.stdout is None`，`print` 抛 `AttributeError`
2. **输出被重定向/管道**：Windows 默认按区域编码（cp936/gbk）打开，`⚠`、`→` 等
   字符抛 `UnicodeEncodeError`

GUI 打开数据库时会打印 WAL 回退等警告，托盘模式把控制台藏起来后更是完全看不到输出，
所以这两条路径都必须在做任何事之前先把流修好。日志只记长度/结果/trace，不记识别文本。
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import paths

LOG_NAME = "voiceime.log"
MAX_BYTES = 2 * 1024 * 1024


def _reconfigure(stream: object) -> bool:
    """把已有流改成 UTF-8；不是 TextIOWrapper（管道包装器/自定义流）就跳过。"""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        return False


def _writable(stream: object) -> bool:
    return stream is not None and hasattr(stream, "write")


def tame_console() -> None:
    """控制台模式：保留现有编码，只把 `errors` 改成 `replace`。

    不能像 `setup()` 那样强制 UTF-8：Windows 控制台的代码页是 936 时，UTF-8 字节
    会整段显示成乱码。这里只保证 `⚠`/`→`/`✅` 这类字符不会把进程打死
    （冻结 exe 的 `--text` 曾在 GBK 控制台上直接 `UnicodeEncodeError` 退出）。
    """
    for stream in (sys.stdout, sys.stderr):
        if not _writable(stream):
            continue
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def setup(force_file: bool = False, name: str = LOG_NAME) -> object | None:
    """确保 `print` 不会因为编码或缺失流而崩。

    - `force_file=True`（托盘）：无论是否有控制台，都写文件（控制台要隐藏）
    - `force_file=False`（GUI）：有可写流就只改编码，没有就落文件

    返回打开的文件句柄（调用方负责关闭），未落文件时返回 None。
    """
    out, err = sys.stdout, sys.stderr
    if not force_file and _writable(out) and _writable(err):
        _reconfigure(out)
        _reconfigure(err)
        return None

    try:
        log_path = paths.data_dir() / name
        if log_path.exists() and log_path.stat().st_size > MAX_BYTES:
            log_path.replace(log_path.with_suffix(log_path.suffix + ".1"))
        handle = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        # 连日志都写不了：至少把现有流改 UTF-8（errors=replace 不会再抛）
        for stream in (out, err):
            _reconfigure(stream)
        return None

    sys.stdout = handle      # type: ignore[assignment]
    sys.stderr = handle      # type: ignore[assignment]
    return handle


def restore(handle: object | None, out: object, err: object) -> None:
    """恢复原流并关闭日志（顺序不能反，否则最后的 print 会写进已关闭文件）。"""
    sys.stdout = out      # type: ignore[assignment]
    sys.stderr = err      # type: ignore[assignment]
    if handle is not None:
        try:
            handle.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def log_path(name: str = LOG_NAME) -> Path:
    return paths.data_dir() / name
