# core —— Python 核（产品里的推理运行时）

不再是"用完就扔的验证件"：交付形态现为 **Tauri 壳（`xifeng.exe`）+ Python sidecar（`Xifeng-Core`）**
（`docs/00-process/plan.md` D14/D16），本目录是 Python 核的源码与工具所在。

它把 `录音 → ASR → 文本清理 → 光标注入` 串起来，并把三个**未实测假设**
变成数字：

1. SenseVoice int8 在 8 核 CPU 上的真实 RTF
2. `SendInput(KEYEVENTF_UNICODE)` 在记事本 / VSCode / 微信里的实际成功率
3. 端到端 e2e 延迟分布（P50/P95）

> `crates/` 的 Rust 实现是正式路径；Python 侧保留逐字节等价的基准实现（parity 测试锁死）。

## 环境

环境由 `uv` 管理（Python 3.11，依赖见 `pyproject.toml`，索引已指向清华镜像）。

```bash
cd core
uv sync                # 首次或依赖变更后
uv run pytest          # 330 个用例，约 30s
```

禁止 `pip install` 到系统 Python：曾因直连 pypi 卡死 6 分钟（见 `plan.md` 偏离 D4）。

## 三件事

### 1. 只跑文本清理（不需要模型、不往窗口打字）

```bash
uv run python run_spike.py --text "呃 今天 我们 嗯 开个会，讨论一下 PyO3。" --no-inject
```

### 2. 完整链路（无模型时自动退回 mock 后端）

```bash
uv run python run_spike.py --ptt --no-inject             # 按住 Ctrl 说话，松开结束
uv run python run_spike.py --ptt --times 3 --no-inject   # 连说 3 次
uv run python run_spike.py --seconds 3 --no-inject       # 录 3 秒
uv run python run_spike.py --interactive --no-inject     # 回车开始 / 回车停止
uv run python run_spike.py --wav sample.wav --no-inject  # 用现成音频
```

PTT 键位：`--key rctrl`（默认，右 Ctrl；左右互斥）/ `lctrl` / `lshift` / `rshift`。

麦克风默认**按下才开、松开就关**（不录音时不占用设备）。Python 走旧 PortAudio 栈，
开流有 100–400ms 静默期，看到"录音中"再开口；壳侧 cpal 实测仅 17–24ms（G23 复测）。
要消掉 Python 这段延迟可加 `--resident-mic`（会一直占用麦克风）。

误触保护：按住不足 `--min-hold-ms 250` 直接丢弃（`Ctrl+C` 不会触发录音）。
录音中按 `Esc` 取消本次。单次上限 `--max-seconds 60`。

输出示例：

```
[trace] outcome=Injected chars=22 asr=0ms post=0ms inject=0ms e2e=0ms
```

### 3. 真注入（会真的往当前光标打字）

先把光标放进记事本，然后：

```bash
uv run python run_spike.py --text "你好，世界😀"     # 不加 --no-inject 就是真打
```

默认 `--inject auto`：先发 Unicode，确认目标没吃下按键才改走剪贴板；
不吃 `WM_CHAR` 的终端/游戏用 `--inject clipboard`，只想发按键用 `--inject unicode`。
剪贴板会在粘贴后恢复原内容（默认等 500ms，`--clipboard-restore-ms` 可调）。

## 下载模型（约 239MiB）

```bash
uv run python fetch_model.py            # 默认 hf-mirror.com，支持断点续传
uv run python fetch_model.py --force    # 强制重下
```

模型落在统一根 `%LOCALAPPDATA%\Xifeng\models\asr\sensevoice-small-int8\`
（`model.int8.onnx` + `tokens.txt`，D15）。也可用 `XIFENG_MODELS_ROOT` 或 `--model-dir` 指定。

## 数据：历史 / 统计 / 设置（SQLite）

默认开着，库在 `%LOCALAPPDATA%\Xifeng\xifeng.db`（Low 进程回退 `<core>/.data`，
冻结 exe 在 `<exe 同级>\data`）。细节见 `docs/03-core-concepts/storage.md`、坑位见 G25。

```bash
uv run python run_spike.py --stats                  # 次数 / 成功率 / P50 / P95
uv run python run_spike.py --history 20             # 最近 20 条（含注入文本）
uv run python run_spike.py --no-db --text "呃 你好"  # 完全不碰数据库
uv run python run_spike.py --no-store-text          # 只记长度，不落文本
```

术语替换已随「不再有术语表」的决策移除，现在只有 `postprocess` 的规则层。

## Web 主界面（五页）

```bash
uv run python run_spike.py --web                 # 默认统计页
uv run python run_spike.py --web --tab config    # 直接开配置页
```

本地服务只监听 `127.0.0.1:8642`，用 Edge `--app` 开无地址栏窗口；关窗 30s 后自动退出。
安装版的同一套页面跑在 Tauri 内嵌 webview 里，不开本地端口。前端源码在 `apps/desktop/`，
改了要先 `vite build`（产物 `apps/desktop/dist`，坑位见 G31）。

## 打包（Tauri 壳 + sidecar）

```bash
cd core
uv run python packaging/build_core.py --clean    # 产物 apps/desktop/src-tauri/resources/core/Xifeng-Core
cd ../apps/desktop
pnpm run tauri:build                             # 壳 + NSIS 安装器
```

产物：`apps/desktop/src-tauri/target/release/xifeng.exe` 与
`apps/desktop/src-tauri/target/release/bundle/nsis/Xifeng_0.1.0_x64-setup.exe`。
慢的是 Rust 编译，不是 Python。`packaging/build.py`（PyInstaller onedir）保留，
只用于单独交付 Python 运行时；模型不进安装包（D15）。
细节与发版见 `docs/02-guides/packaging.md`（坑位 G24/G37/G38）。

## 延迟基准

```bash
uv run python bench.py --n 20 --seconds 3                    # mock，只验证管线开销
uv run python bench.py --n 20 --wav sample.wav --backend sherpa   # 真实 ASR
```

判据来自 `docs/01-overview/latency-budget.md`：ASR ≤350ms、postprocess <1ms、
e2e P95 ≤1500ms。**mock 后端的 ASR 恒为 0，只能证明管线没问题，不能证明推理快。**

## 按住 Ctrl 没反应？

```bash
uv run python key_diag.py
```

不需要麦克风、不加载模型。会打印完整性级别/会话、两种钩子线程模型的事件计数、
Tk 窗口能否收到按键（判据）、以及按住 Ctrl 的真实时长。

- `完整性=Low(0x1000)` → 本工作区目录带 Low 标签，目录内的 exe 都拿不到硬件输入（G21）。
  新建目录外的 venv 即可绕开：`uv venv %TEMP%\vi-venv --python 3.11` +
  `set PYTHONPATH=.venv\Lib\site-packages` + `%TEMP%\vi-venv\Scripts\python.exe key_diag.py`
- 钩子 0 事件但 Tk 能收键（且 Medium）→ 全局钩子被安全软件拦，试管理员运行
- 事件数 > 0 且"注入"占多数 → 键盘经过重映射软件，已兼容（G20）

## 目录

| 文件 | 作用 |
|---|---|
| `xifeng/trace.py` | Trace 结构、JSONL 落盘、分位数 |
| `xifeng/postprocess.py` | 幻觉过滤 / 语气词 / 归一化（Rust 侧的行为基准） |
| `xifeng/vad.py` | 能量 VAD 基准实现（壳侧用 Rust `ime-vad` + Silero，parity 测试锁死） |
| `xifeng/asr.py` | ASR 适配层：mock / sherpa / auto |
| `xifeng/hotkey.py` | 源码调试用的全局 PTT（正式触发在壳里） |
| `key_diag.py` | 按键链路诊断：环境/钩子两种线程模型/Tk 判据 |
| `xifeng/inject.py` | 焦点快照 + SendInput(UNICODE) + 剪贴板兜底（`auto`） |
| `xifeng/store.py` | SQLite：历史 / 统计 / 设置（G25） |
| `xifeng/paths.py` | 模型与数据目录解析（源码/冻结两套，G24） |
| `xifeng/ipc.py` | 壳 ↔ Python 的 NDJSON 方法表 |
| `xifeng/webgui.py` | Web 调试界面 + HTTP API（更新检查在这里） |
| `packaging/build_core.py` | sidecar 打包（产物进 `apps/desktop/src-tauri/resources/core`） |
| `packaging/build.py` | PyInstaller 打包（仅单独交付 Python 运行时） |
| `../apps/desktop/` | React 五页 + `src-tauri`（Rust 壳） |
| `xifeng/pipeline.py` | 串联 + 打点 |
| `xifeng/audio.py` | WAV 读写、合成音频 |
| `run_spike.py` | 手动跑一次 |
| `bench.py` | P50/P95/P99 基准 |
| `inject_smoke.py` | 注入 smoke：开 Tk 窗口 → 注入 → 读回比对（**必须在用户终端里跑**，见下） |
| `quality_check.py` | 真人语音质量：固定 5 句 → CER |
| `fetch_model.py` | 模型下载（国内镜像） |

> `inject_smoke.py` 要抢前台，MUST 用目录外的 Medium 解释器跑；Low 进程会一律被拒（G14）。
> 请在自己的终端执行 `uv run python inject_smoke.py`，期望 `5/5 通过`。

## 壳与 Python 的分工

| 能力 | 壳（Rust） | Python |
|---|---|---|
| 热键 + 组合键观察窗 | `WH_KEYBOARD_LL` | `hotkey.py`（仅源码调试） |
| 采集 | cpal，按需开流 | sounddevice（`--ptt` 调试回路） |
| VAD / 后处理 | `ime-vad` / `ime-post` | `vad.py` / `postprocess.py`（基准） |
| 注入 | SendInput | `inject.py`（同一套语义） |
| ASR / 纠错 | — | SenseVoice / Qwen2.5-0.5B |
