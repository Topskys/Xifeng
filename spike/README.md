# spike —— 交付主程序（Python 实现）

不再是"用完就扔的验证件"：2026-10-29 决定交付形态为 **Python + PyInstaller**
（见 `docs/00-process/plan.md` 偏离 D6），本目录就是主程序。

它把 `录音 → ASR → 文本清理 → 术语表 → 光标注入` 串起来，并把三个**未实测假设**
变成数字：

1. SenseVoice int8 在 8 核 CPU 上的真实 RTF
2. `SendInput(KEYEVENTF_UNICODE)` 在记事本 / VSCode / 微信里的实际成功率
3. 端到端 e2e 延迟分布（P50/P95）

> `crates/` 的 Rust 版保留为后续可选方案；行为基准仍是这里的 `postprocess` / `glossary`。

## 环境

环境由 `uv` 管理（Python 3.11，依赖见 `pyproject.toml`，索引已指向清华镜像）。

```bash
cd spike
uv sync                # 首次或依赖变更后
uv run pytest          # 328 个用例，约 25s
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

麦克风默认**按下才开、松开就关**（不录音时不占用设备）。代价是开流有 100–400ms
静默期，看到"录音中"再开口；要消掉这段延迟可加 `--resident-mic`（会一直占用麦克风）。

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

模型落在 `../models/asr/sensevoice-small-int8/`（`model.int8.onnx` + `tokens.txt`）。
也可用 `IME_MODELS_DIR` 或 `--model-dir` 指定。

## 数据：历史 / 统计 / 术语表（SQLite）

默认开着，库在 `%LOCALAPPDATA%\Xifeng\xifeng.db`（Low 进程回退 `<spike>/.data`，
冻结 exe 在 `<exe 同级>\data`）。细节见 `docs/03-core-concepts/storage.md`、坑位见 G25。

```bash
uv run python run_spike.py --stats                  # 次数 / 成功率 / P50 / P95
uv run python run_spike.py --history 20             # 最近 20 条（含注入文本）
uv run python run_spike.py --terms                  # 术语表 + 命中次数
uv run python run_spike.py --add-term "git hub=GitHub"
uv run python run_spike.py --del-term "read me"
uv run python run_spike.py --no-db --text "呃 你好"  # 完全不碰数据库
uv run python run_spike.py --no-store-text          # 只记长度，不落文本
```

术语表与复读折叠在注入前自动生效（`glossary.py`）：`voice i e` → `Xifeng`、
`read me` → `README`、`就交就提交` → `就提交`。
`--text` 模式也会跑一遍，方便调规则（打印 `[glossary] fixes=... hits=...`）。

## Web 主界面（五页）

```bash
uv run python run_spike.py --web                 # 默认统计页
uv run python run_spike.py --web --tab terms     # 配置页并定位术语表
```

本地服务只监听 `127.0.0.1:8642`，用 Edge `--app` 开无地址栏窗口；关窗 30s 后自动退出。
前端源码在 `frontend/`，改了要先 `vite build`（产物 `frontend/dist`，坑位见 G31）。

## 打包成 exe（PyInstaller，onedir）

```bash
# 本仓库目录是 Low 完整性，必须用仓库外的 Medium 解释器构建
uv venv %TEMP%\vi-venv --python 3.11
set PYTHONPATH=D:\project\workspace\voice-ime\spike;D:\project\workspace\voice-ime\spike\.venv\Lib\site-packages
%TEMP%\vi-venv\Scripts\python.exe packaging\build.py
```

产物在 `%LOCALAPPDATA%\Xifeng-build\dist\Xifeng\`（145.7MB，exe 5.6MB；带模型 374.7MB）。
模型外置：`<exe 同级>\models\asr\sensevoice-small-int8` 或 `IME_MODELS_DIR`。
同一份 exe 支持 `--tray`（托盘常驻）/ `--web`（Web 五页）/ `--gui`（旧版兜底）/ `--autostart on`。
`--web` 需要 `<exe 同级>\frontend`，`build.py` 会自动复制。
细节与验收清单见 `docs/02-guides/packaging.md`（坑位见 G24）。

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
| `xifeng/vad.py` | 能量 VAD 替身，验证切分/合并/padding（M1 换 Silero） |
| `xifeng/asr.py` | ASR 适配层：mock / sherpa / auto |
| `xifeng/hotkey.py` | 全局 PTT：低级键盘钩子（兜底轮询）+ 误触保护 |
| `key_diag.py` | 按键链路诊断：环境/钩子两种线程模型/Tk 判据 |
| `xifeng/inject.py` | 焦点快照 + SendInput(UNICODE) + 剪贴板兜底（`auto`） |
| `xifeng/store.py` | SQLite：历史 / 统计 / 术语表 / 设置（G25） |
| `xifeng/glossary.py` | 术语替换 + 重启复读折叠（纯字符串） |
| `xifeng/paths.py` | 模型与数据目录解析（源码/冻结两套，G24） |
| `packaging/build.py` | PyInstaller 打包脚本（onedir，产物落仓库外） |
| `xifeng/pipeline.py` | 串联 + 打点 |
| `xifeng/audio.py` | WAV 读写、合成音频 |
| `run_spike.py` | 手动跑一次 |
| `bench.py` | P50/P95/P99 基准 |
| `inject_smoke.py` | 注入 smoke：开 Tk 窗口 → 注入 → 读回比对（**必须在用户终端里跑**，见下） |
| `quality_check.py` | 真人语音质量：固定 5 句 → CER |
| `fetch_model.py` | 模型下载（国内镜像） |

> `inject_smoke.py` 要抢前台，MUST 用目录外的 Medium 解释器跑；Low 进程会一律被拒（G14）。
> 请在自己的终端执行 `uv run python inject_smoke.py`，期望 `5/5 通过`。

## 与正式实现的差异（都是有意的）

| 差异 | spike 做法 | 正式要求 |
|---|---|---|
| 热键 | 回车/固定秒数/Ctrl 长按 | rdev 全局热键（Ctrl 长按，Esc 取消） |
| 音频回调 | `list.append`（会分配） | 预分配环形缓冲，回调内禁止分配/加锁 |
| VAD | 能量阈值 | Silero VAD v5（Rust ort，30ms 块） |
| 纠错 | 无 | L0 规则 + L1 LLM（M2） |
| 存储 | SQLite（历史/术语/设置） | 同左 |
