# 架构与进程模型

本文件描述 Xifeng 的进程划分、模块归属与技术选型理由。变更架构前 MUST 先读本文件并同步更新。

> **现行架构（D14 2026-11-09，D16 2026-11-13 定稿）**：**Tauri 壳（Rust）管触发链，
> Python sidecar 只做推理。**
> 壳：`WH_KEYBOARD_LL` 热键 → cpal 按需开麦 → 环形缓冲 → 喂送线程 → IPC；松手后
> `ime-post` 后处理 → `SendInput` 注入。托盘、自启、五页 webview、录音浮窗也在壳里。
> Python：`--ipc` stdio NDJSON（`asr_begin/feed/end/cancel`），吃 PCM 吐原始文本；
> 历史/设置/统计仍由 Python 侧的 SQLite 承担（壳经 `session_record` 落一条）。
> 旧的 Python `--ptt --tray` 语音服务已退役（D16），只保留为源码调试回路。

## 进程模型（现状）

| 进程 | 实现 | 职责 | 崩溃策略 |
|---|---|---|---|
| `xifeng.exe` | Tauri 2 + Rust | 热键与 150ms 组合键观察窗、cpal 采集与 drain、`ime-post` 后处理、`SendInput` 注入、录音浮窗、托盘、自启、设置读取 | 崩溃后由自启/用户重新拉起；进程内不持久化会话 |
| `Xifeng-Core.exe` | Python 3.11（PyInstaller onedir） | SenseVoice ASR、静音裁剪/VAD、可选 Qwen 纠错、SQLite（历史/设置/统计）、`--web` 调试界面 | 壳检测到退出会显式报错（不静默返回空文本）；下一次会话由壳重新拉起 |

两进程只通过 **stdio NDJSON** 交互（契约见 `03-core-concepts/ipc-contract.md`），
不共享内存、不开端口（`--web` 调试模式例外，仅绑定 `127.0.0.1:8642`）。

## 壳内模块（`apps/desktop/src-tauri/src/`）

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `hotkey.rs` | `WH_KEYBOARD_LL` 钩子；左右互斥；`pressed`/`drain_presses`；忽略带魔数的自家注入 | 回调内不加锁、不做 IO（G20） |
| `voice.rs` | 编排：按下 → 观察窗 → 弹浮窗 ∥ `mic.start()` → 录音循环 → drain → `asr_end` → `ime-post` → 注入 → `session_record` | 浮窗与开流并行（D20）；组合键判定回传（G42） |
| `inject.rs` | 焦点快照、`build_events`、SendInput(UNICODE)、剪贴板兜底、提权拒绝 | UTF-16 码元；换行 `VK_RETURN`（G2/G3/G7/G13） |
| `py.rs` | sidecar 生命周期、NDJSON 读写、单次 RPC 超时、`env_compat` | stderr 按 UTF-8 读行（G43）；`VOICEIME_*` 只读兼容 |
| `tray.rs` | 托盘两项：显示主界面 / 退出 | 不起第二个实例 |
| `autostart.rs` | 写 `HKCU\...\Run\Xifeng`，删旧值 `VoiceIME` | 只写 HKCU，免 UAC |
| `main.rs` | 窗口（主界面 + `?window=overlay`）、命令注册、启动预热 | 模式切换在**下一次会话**生效 |

## Python 侧模块（`core/xifeng/`）

| 文件 | 职责 | 说明 |
|---|---|---|
| `ipc.py` | NDJSON 方法表与分发（`asr_begin/feed/end/cancel`、`bootstrap`、`settings_*`、`correct`、`session_record`、`check_update` …） | 与 `webgui.Api` 一一对应 |
| `asr_runtime.py` | sidecar 会话：收 PCM → 裁剪/VAD → 解码 → 返回原始文本 | 不做后处理（后处理在壳） |
| `asr.py` | sherpa-onnx / SenseVoice 适配（mock / auto 兜底） | 独立线程推理 |
| `correction.py` | 本地 Qwen2.5-0.5B + 守卫（默认关，G32） | 输出必须先过 `check` |
| `vad.py` | 能量/Silero VAD 与首尾裁剪，按**整句**调用一次 | 基准实现；热点可走 `xifeng_rs` |
| `postprocess.py` | 幻觉/语气词/归一化，纯 Python 基准 | 与 `crates/ime-post` 逐字节等价（parity 测试） |
| `store.py` | SQLite：历史 / 统计（G25） | 库在 `%LOCALAPPDATA%\Xifeng\xifeng.db` |
| `config.py` | 外置 JSON 配置读写 + 旧 SQLite 设置迁移 | 文件在 `%LOCALAPPDATA%\Xifeng\config.json` |
| `paths.py` | 数据/模型目录解析，旧 `VoiceIME` 迁移（D21/G43） | `XIFENG_*` 环境变量 |
| `webgui.py` | HTTP 调试界面 + `check_update`（GitHub latest release） | 只绑 `127.0.0.1` |
| `settings.py` / `autostart.py` / `applog.py` | 设置校验、源码调试用自启、日志 | 壳侧各有对应实现 |

`--gui`（tkinter 四页）与 `--tray` 仍可用，仅作源码调试；安装版不经过它们。

### 配置与语言（0.2.0）

- **设置外置成 JSON**：`config.py` 负责原子读写、坏文件备份、旧库迁移；
  `settings.py` 只管校验与类型转换。用户在配置页就能看到文件路径并直接编辑。
- **识别语言可切换**：`settings.language` ∈ {zh, en, ja, ko, yue}。
  语言是 sherpa recognizer 的**构造参数**，改了必须重建模型（数秒）——
  `settings_save` 会自动调一次 IPC `set_language` 完成重建，用户不必手动操作。
  必须显式指定：G22 记录过 `auto` 会把普通话判成粤语（CER 57% → 0%）。

## 线程模型

```
壳主线程   ─ Tauri 事件循环（热键回调、窗口、IPC 调用）
音频回调   ─ cpal：只把一块 PCM 拷进 SPSC 环形缓冲（禁止 IO/锁/日志/推理）
喂送线程   ─ 环 → 20ms 块 → base64 → asr_feed（会话中）
注入路径   ─ ime-post → SendInput 序列（块间 5ms）/ 剪贴板兜底
Python 侧  ─ 单进程：asr_end 内同步裁剪/VAD/解码；纠错在独立线程；DB 访问走同一进程
```

约束：

- 音频回调里 MUST NOT 持锁等待、MUST NOT 调用 Python
- 推理 MUST 在独立线程；壳侧不得在等 IPC 时阻塞音频回调
- 同一时刻只允许一个语音会话；设置热更在会话之间生效

## 数据流（一次输入）

```
热键按下 → cpal 开流 → 环形缓冲 → 喂送线程 → asr_begin/asr_feed
  松手 → drain 120ms → asr_end →（Python：裁剪/VAD + SenseVoice 解码）
  → 原始文本 →（可选）correct + 守卫 → ime-post（壳）
  → SendInput(UNICODE) 注入 → session_record → SQLite / 日志 / GUI
```

空结果不注入；提权目标显式失败；日志只记长度与 outcome。

## 技术选型（已定，变更需评审）

| 点 | 选型 | 理由 | 备选 |
|---|---|---|---|
| ASR | SenseVoice-Small ONNX int8 | 中文强、自带标点/ITN、CPU RTF≈0.07 | Paraformer、Faster-Whisper |
| VAD | Silero v5（`xifeng/vad.py`，可走 `xifeng_rs`） | 按句调用一次，不进音频回调 | FSMN-VAD、纯能量 |
| 纠错 | Qwen2.5-0.5B GGUF Q4（本地）+ 守卫 | CPU 热身后 0.35–1.5s/句，可整段关掉；守卫兜住小模型乱改 | GLM-Edge、云 API |
| 桥接 | 壳 ↔ Python：stdio NDJSON | 无端口、无序列化框架；IPC 契约可测 | Named Pipe、gRPC |
| 进程内热点 | PyO3（`xifeng_rs`：`ime-post` / `ime-vad`） | 零序列化开销，Python 保留等价基准 | 纯 Python |
| GUI | Tauri 2 内嵌 webview（五页） | 与壳同进程，无需本地端口；React 复用 | Edge `--app` + HTTP（仅调试） |
| 音频 | cpal + SPSC 环形缓冲 | 成熟、`play→首回调` 17–24ms | PortAudio（旧栈 95–376ms） |
| 注入 | SendInput (UNICODE) + 剪贴板兜底 | 正常情况免切输入法、不碰剪贴板；不吃 `WM_CHAR` 的目标自动降级 | 纯剪贴板、纯 IME |
| 托盘 | Tauri `tray-icon` | 与壳同进程，不引 pystray | pystray（Python 调试） |
| 开机自启 | `HKCU\...\Run\Xifeng` | 免 UAC、卸载删一个值即可 | 启动文件夹 .lnk |

## VAD 为什么按句做，而不是 30ms 流式

流式 VAD 每 30ms 一次，一天几千次调用；跨进程/跨语言边界的固定开销会吃掉预算，
还会让音频链路依赖另一个进程的存活。当前实现是在 `asr_end` 里对**整段音频**做一次
VAD 与首尾裁剪（O(n)，毫秒级），足够解决"长静音幻觉"和"首尾空白"。

如果将来要"边录边切句"（流式上屏），VAD MUST 留在壳里跑 `crates/ime-vad`
（算法与 Python 基准等价，有 parity 测试），MUST NOT 每 30ms 发一次 IPC。
