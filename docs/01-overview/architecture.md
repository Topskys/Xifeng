# 架构与进程模型

本文件描述 VoiceIME 的进程划分、模块归属与技术选型理由。变更架构前 MUST 先读本文件并同步更新。

> **2026-10-29 决策（D6）**：交付形态改为 **Python 单进程 + PyInstaller**，Rust 版推迟。
> 下文的 Rust 目标态保留为"后续可选"，当前实现见 `spike/voiceime_spike/`：
> `hotkey → LiveMic → asr(sherpa) → post → inject`，模块边界与下表一一对应。
>
> **2026-11-09 修订（D13）**：Rust **以 PyO3 扩展参与**，Python 仍是主流程。
> 已落地部分是后处理：`crates/ime-post`（规则，纯 Rust）→ `crates/ime-py`（`voiceime_rs.pyd`，
> abi3-py311）。Python 侧 `voiceime_spike/postprocess.py` 保留纯 Python 基准实现，
> 默认自动走 Rust；`cargo test -p ime-post` 与 `tests/test_postprocess_parity.py` 两侧都校验。
> 热键钩子 / 音频环 / VAD / 注入属于第 2 阶段。

## 进程模型（目标态）

| 进程 | 实现 | 职责 | 崩溃策略 |
|---|---|---|---|
| `ime-service` | Rust (tokio) | 热键、音频采集、VAD、PyO3 推理、后处理、注入、模型管理、IPC 服务端 | 自动重启 1 次/分钟；状态从磁盘恢复 |
| `ime-gui` | Tauri 2 | 统计 / 模型 / 配置 / 历史 / 关于 五页 + IPC 客户端 | 独立崩溃不影响输入功能 |

两进程 MUST NOT 共享内存以外的状态；一切交互走 JSON-RPC over Named Pipe `\\.\pipe\voiceime`。

> **当前 Python 形态（D6/D7）**：一个 `VoiceIME.exe` 同时承担 service + tray，
> `--tray` 时隐藏控制台并常驻；主界面用 `--web` **另起一个进程**（本地 HTTP + Edge 应用窗口，
> `spike/frontend` 的 React 五页），共享同一个 SQLite 文件而不是 Named Pipe。
> Web 崩了不影响输入，PTT 退出也不影响看历史。
> 两个 resident 实例由命名 Mutex 挡住。

## ime-service 内部模块

| crate | 职责 | 关键依赖 | M0 |
|---|---|---|---|
| `ime-core` | 管线状态机、Trace、降级决策 | tokio, crossbeam | ✅ |
| `ime-audio` | cpal 采集 16k/mono、环形缓冲 | cpal | ✅ |
| `ime-vad` | Silero VAD v5（ort）、双阈值切分、段重组 | ort | ❌ M1 |
| `ime-infer` | PyO3 嵌入；ASR 与 LLM | pyo3, numpy | ✅（仅 ASR） |
| `ime-post` | TextPostProcessor：幻觉/语气词/归一化 | regex | ✅ |
| `ime-correct` | LLM 纠错编排 + 守卫（Python 版已落地，默认关） | — | ❌ M2 |
| `ime-inject` | 焦点快照、SendInput / 剪贴板 | windows, arboard | ✅ |
| `ime-registry` | 模型清单、下载校验、热切换 | reqwest, sha2, arc-swap | ❌ M3 |
| `ime-hotkey` | 全局热键（rdev，Ctrl 长按 + Esc 取消） | rdev | ✅ |
| `ime-ipc` | Named Pipe JSON-RPC | tokio | ❌ M3 |

## 当前 Python 实现（交付形态）

上表是**目标态**（Rust，推迟）。当前代码与它的对应关系：

| 文档里的概念 | 实际文件 | 说明 |
|---|---|---|
| `ime-hotkey` | `voiceime_spike/hotkey.py` | `WH_KEYBOARD_LL` + 轮询兜底 + 按需开麦（G23） |
| `ime-audio` | `voiceime_spike/audio.py` + `hotkey.LiveMic` | sounddevice 16k i16，按下才开流、回调只入队 |
| `ime-infer` | `voiceime_spike/asr.py` | sherpa-onnx / SenseVoice；独立线程推理 |
| `ime-post` | `voiceime_spike/postprocess.py` | 幻觉/语气词/归一化 |
| `ime-correct`（Python 版） | `voiceime_spike/correction.py` | 本地 Qwen2.5-0.5B + 守卫（默认关，G32） |
| `ime-inject` | `voiceime_spike/inject.py` | 焦点快照 + SendInput(UNICODE) + 剪贴板兜底（`auto`） |
| Trace | `voiceime_spike/trace.py` | JSONL + 分位数 |
| 存储（原 M3） | `voiceime_spike/store.py` | SQLite：历史/统计/设置 |
| 路径解析 | `voiceime_spike/paths.py` | 源码/冻结两套（G24/G25） |
| 托盘 / 自启 | `voiceime_spike/{tray,autostart}.py` | pystray 菜单、命名 Mutex、`HKCU\...\Run` |
| GUI（M3） | `spike/frontend` + `voiceime_spike/webgui.py` | React + shadcn 五页；`127.0.0.1:8642` + Edge `--app`（G31） |
| 旧 GUI（兜底） | `voiceime_spike/gui.py` | tkinter 四页，`--gui` 仍可用 |
| 应用日志 | `voiceime_spike/applog.py` | `--tray` 隐藏控制台后的 stdout/stderr（UTF-8 文件） |
| `ime-service` 骨架 | `run_spike.py` + `pipeline.py` | 状态机、Trace、CLI、`--tray` 常驻 |

## 线程模型

```
主线程       ─ tokio runtime（IPC、模型下载、事件）
音频线程     ─ cpal 回调，仅入队（禁止阻塞）
VAD 线程     ─ 30ms 块消费，Rust 直跑 Silero，输出段事件（M1）
推理线程     ─ Python 解释器所在线程（GIL 持有者），消费 PCM 队列
注入线程     ─ SendInput 序列发送（块间 5ms）/ 剪贴板兜底
```

约束：

- Python 解释器 MUST 在启动时于推理线程初始化一次，之后 MUST NOT 迁移线程（PyO3 要求）
- 音频回调中 MUST NOT 持锁等待、MUST NOT 调用 Python

## 数据流（一次输入）

```
热键按下 → 采集(16k i16) → 首尾静音裁剪 → ASR
  → TextPostProcessor →（可选）模型纠错 + 守卫 → Injector(光标)
  → Trace 汇总 → SQLite(history) / 日志 → Web GUI(随时读同一个库)
```

VAD 切分仍是 M1 才引入；当前用"按键起止 + 能量法首尾裁剪"。
纠错默认关（每句 +0.35–1.5s）；开启后其输出必须先过 `correction.check` 守卫，
拒绝就注入清理后的原文（G32）。

## 技术选型（已定，变更需评审）

| 点 | 选型 | 理由 | 备选 |
|---|---|---|---|
| ASR | SenseVoice-Small ONNX int8 | 中文强、自带标点/ITN、CPU RTF≈0.07 | Paraformer、Faster-Whisper |
| VAD | Silero v5（Rust ort） | 30ms 块 <1ms，不进 Python | FSMN-VAD、能量 VAD |
| 纠错 | Qwen2.5-0.5B GGUF Q4（本地）+ 守卫 | CPU 热身后 0.35–1.5s/句，可整段关掉；守卫兜住小模型乱改 | GLM-Edge、云 API |
| 桥接 | PyO3 嵌入 | 零序列化开销 | 独立进程 + gRPC |
| GUI | 当前 Web（vite+React+shadcn，D7） / 目标 Tauri 2 | Edge `--app` 免 WebView 依赖，界面即 Web 技术栈 | Tauri、Qt、tkinter |
| 音频 | cpal + 环形缓冲 | 成熟、低延迟 | — |
| 注入 | SendInput (UNICODE) + 剪贴板兜底 | 正常情况免切输入法、不碰剪贴板；不吃 `WM_CHAR` 的目标自动降级 | 纯剪贴板、纯 IME |
| 托盘 | pystray + Pillow | 纯 ctypes 调 Shell_NotifyIcon，不引 pywin32 | 自写 Win32 |
| 开机自启 | `HKCU\...\Run` | 免 UAC、卸载删一个值即可 | 启动文件夹 .lnk |

## 为什么不把 VAD 放 Python

VAD 每 30ms 一次，一天几千次调用。跨语言边界 + GIL + numpy 转换的固定开销会吃掉预算，
且会让音频链路依赖 Python 进程存活。**高频小计算留 Rust，低频大计算进 Python** 是硬边界。
