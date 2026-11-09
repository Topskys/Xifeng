# AGENTS.md — VoiceIME

本地语音输入法。**Python 主流程**（sounddevice + ctypes Win32）+ SenseVoice ASR，
PyInstaller 打包交付。Rust 版推迟（决策见 `docs/00-process/plan.md` 偏离 D6）。
**端到端 P95 ≤ 1500ms 是硬指标**（实测 P95 582ms）。

## 命令

```bash
# 全部命令在 spike/ 下执行（uv 管理环境，Python 3.11，镜像已配清华源）
cd spike
uv sync                                  # 首次/依赖变更后
uv run pytest                            # 单测（219 个，<15s）
uv run python run_spike.py --help        # 手动跑一次
uv run python bench.py --n 20            # 输出各阶段 P50/P95
uv run python quality_check.py --wav-dir fixtures/quality   # 复算 CER
uv run python ptt_selftest.py            # PTT 时序自测（需 Medium 完整性）
uv run python run_spike.py --web         # 打开 Web 主界面（五页）
uv run python run_spike.py --tray        # 托盘常驻（默认按需开麦）

# 打包（MUST 用仓库外的 Medium 解释器，见 docs/02-guides/packaging.md）
%TEMP%\vi-venv\Scripts\python.exe packaging\build.py
```

> Python 环境 MUST 用 `uv`（`spike/pyproject.toml` + `spike/.venv`）。
> 禁止 `pip install` 到系统 Python；禁止提交 `.venv` 与 `__pycache__`。
> 本仓库目录带 **Low 完整性标签**：从这里启动的 exe 收不到硬件按键（G21），
> 打包产物 MUST 落在仓库外的目录（G24）。

## 硬约束（违反即打回）

- 音频回调里 **禁止** IO、日志、推理、加锁；只允许"拷贝一块 → 入队/入环形缓冲"
- 音频/文本 **禁止** 离开本机；唯一网络请求是模型下载
- 注入前 MUST 校验焦点仍在快照窗口；提权目标 MUST 显式失败，禁止静默
- 换行用 `VK_RETURN`，**禁止** 发送 Unicode `\n`
- 注入文本 MUST 经过 `postprocess`；开启纠错时模型输出 MUST 通过守卫（删词/大改/解释性输出一律丢弃）；空结果不注入
- 剪贴板兜底 MUST 恢复原内容；若期间剪贴板已被改写则 MUST NOT 覆盖
- 日志禁止记录识别文本全文，只记长度与 outcome
- 推理 MUST 在独立线程，**禁止** 在音频回调里调用 ASR
- 不录音时 **禁止** 占用麦克风：默认按下热键才 `LiveMic.start()`，松开后关闭

## 架构

```
hotkey(WH_KEYBOARD_LL) → 按需开麦(sounddevice, 16k i16) → asr(sherpa-onnx/SenseVoice)
        └──────── pipeline: 状态机 + Trace ────────┘ → post → inject(SendInput)
        托盘/统计/配置/历史/关于 = Web GUI(127.0.0.1:8642 + Edge --app)
```

## 常见错误（改这里之前先看）

- 不要用 `str` 逐字符注入 —— 必须 `encode("utf-16-le")`，否则 emoji 乱码
- 不要"按下才开流"却又想要零延迟 —— 开流有 95–376ms 静默期；默认按需开麦必须接受这点，要消掉只能在明确接受占麦后加 `--resident-mic`（G23）
- 不要松手立刻关流 —— 会丢最后 150–300ms，必须 `drain_ms`（G23）
- 不要在托盘菜单里起 tkinter —— 主界面是 Web（`--web --tab`），`--gui` 只是旧版兜底
- 不要按次加载模型 —— 模型常驻，启动 warmup 一次
- 不要把模型打进 onefile —— 228MB 每次启动解压；模型外置（G24）
- 不要把纠错模型复制进 dist —— 放 `%LOCALAPPDATA%\VoiceIME\models\llm`（重建 dist 不丢，G25 同理）
- 不要在仓库目录构建 exe —— 产物继承 Low 标签，钩子失效（G24）
- 改 VAD 阈值 / 分词规则必须跑 golden 用例，否则会静默误伤
- 不要为了"便携"把 SQLite 放 exe 同级 —— 重建 dist 会清空（G25）
- 不要让小模型"自由改写" —— 守卫必须拒绝删词/大改/解释性输出，宁可漏改（G32）
- 不要删产物里的 `msvcp140.dll` 修崩溃 —— 用 `build.py` 按版本号覆盖成系统的新版（G33）
- 不要指望 DWM 亚克力能给 Tk 做毛玻璃 —— 浮窗是"截图 + 高斯模糊"（BitBlt 抓 300×32，G34）
- 不要全局杀 `msedge` 或复用用户的浏览器窗口 —— 验证 MUST 用独立 `--user-data-dir` + 独立调试端口 + `--new-window`，收尾只关自己启动的 PID/profile
- 不要往仓库根目录加文档 —— 除本文件外，文档一律进 `docs/`

## 文档

- 入口：`docs/llms.txt`
- 开发流程（闸门/DoD/反模式）：`docs/00-process/sdlc.md`
- 通用范式（跨项目参考）：`docs/reference/ai-native-sdlc-playbook.md`
- 改延迟相关代码前必读：`docs/01-overview/latency-budget.md`
- 改注入/音频/后处理前必读：`docs/05-gotchas.md`
- 打包/交付：`docs/02-guides/packaging.md`

## 目录约定

```
voice-ime/
├── AGENTS.md          # 只放这一个文档
├── docs/
│   ├── llms.txt
│   ├── 00-process/    # intent / spec / plan / review / sdlc（SDLC 工件）
│   ├── 01-overview/   # architecture / latency-budget
│   ├── 02-guides/     # quickstart / packaging
│   ├── 05-gotchas.md
│   └── reference/     # 闲聊.md 等原始素材
├── spike/             # 交付主程序（uv 项目）+ packaging/
└── crates/            # Rust 版（推迟，见 D6）
```
