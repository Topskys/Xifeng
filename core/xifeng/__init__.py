"""Xifeng Python 运行时 —— ASR / 后处理 / 纠错推理，经 IPC 供 Tauri 壳调用。

历史：本包最初是 plan.md Step 0 的 spike（先不装 Rust 工具链，用 Python 把
「录音 → ASR → 文本清理 → 注入」跑通并拿到真实延迟数字）。D16 之后触发链
移到壳里，这里只保留推理与数据层。
"""

__version__ = "0.3.0"
