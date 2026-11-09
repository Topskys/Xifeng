"""VoiceIME Python spike —— 仅用于验证假设，不是最终实现。

对应 plan.md Step 0：在装 Rust 工具链前，先用 Python 把
「录音 → ASR → 文本清理 → 注入」跑通，并拿到真实延迟数字。
接口形状刻意与 spec.md §4 保持一致，便于之后平移到 Rust。
"""

__version__ = "0.1.0"
