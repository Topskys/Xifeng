# Plan: M0 — 走通一次语音输入

Status: **M0 已闭环**；M3（GUI）与 M4（托盘/自启/无窗口）已增量完成（2026-10-29）  
Spec: `spec.md`（2026-10-28）  
判据: `spec.md` §7（A0-1 ~ A0-7）

---

## 0. 前置：本机现状

| 项 | 现状 | 影响 |
|---|---|---|
| Rust 工具链 | **未安装** | 不能直接开始 Rust 实现 |
| Python | 3.13.3（venv 内 3.11.15） | 可用；3.13 与最终 embeddable 3.11 不同，仅用于诊断 |
| Node / pnpm | 24.6 / 12.4 | 可用（M3 才需要） |
| 麦克风 | ✅ 已验证（真人 5 句录音，4.6–8.0s） | Step 4 可用同一批 wav 做回归 |
| 模型文件 | ✅ SenseVoice int8（239MiB）+ tokens.txt | Step 5 可直接比数 |

**决策**：先做 **Step 0 的 Python spike**，用同一套接口契约（`spec.md` §4）验证三个未测量假设，再决定是否按原设计进入 Rust。这是"先降不确定性，再写正式代码"。

---

## Step 0 · Python spike（先跑起来）

**目的**：在不装 Rust 的前提下，验证 spec 里三个 Open Question。

| 子步骤 | 产出 | 验证 | 状态 |
|---|---|---|---|
| 0.1 目录 + Trace 结构 | `spike/xifeng/trace.py` | 能输出各阶段耗时 | ✅ |
| 0.2 文本清理 | `spike/xifeng/postprocess.py` | 单测全绿 | ✅ |
| 0.3 能量 VAD（临时替身） | `spike/xifeng/vad.py` | 能切出语音段 | ✅ |
| 0.4 ASR 适配 | `spike/xifeng/asr.py` | 有模型走 sherpa-onnx，无模型走 mock | ✅ |
| 0.5 注入 | `spike/xifeng/inject.py` | `inject_smoke.py` 5/5 | ✅ chunk=32 与 chunk=8 均 5/5 |
| 0.6 管线串联 | `spike/xifeng/pipeline.py` | 一条命令跑通 | ✅ |
| 0.7 延迟 bench | `spike/bench.py` | 输出 P50/P95 | ✅ 空载/带载均已测 |
| 0.8 模型下载 | `spike/fetch_model.py` | 239MiB 断点续传 | ✅ |
| 0.9 环境 | `spike/pyproject.toml`（uv + 清华源） | `uv sync` <30s | ✅ |
| 0.10 识别质量（CER） | `spike/quality_check.py` | 真人读 5 句，平均 CER ≤15% | ✅ 3.3%（3/5 全对）；语言已固定 zh |
| 0.11 全局 PTT 键 | `spike/xifeng/hotkey.py` | 按住 Ctrl 说话、松开结束 | ✅ **物理键已实测**（2026-10-29 22:07，冻结 exe，`mode=ptt` 落库，16.6s 音频 ASR 1190ms）；自注入 e2e 2/2、`ptt_selftest.py` 4/4 |
| 0.12 按键链路诊断 | `spike/key_diag.py` | 一次跑完给出"读不到键"的根因 | ✅ 已定位：工作目录带 Low 完整性（G21） |
| 0.13 打包交付（Python） | `spike/packaging/build.py` | 双击即用，PTT 在 Medium 下可用 | ✅ onedir 145.6MB（+模型 374.1MB），启动 1.98s，PTT 物理键已实测（G24） |
| 0.14 SQLite 数据层 | `spike/xifeng/store.py` | 历史/统计/术语/设置可读写，迁移幂等 | ✅ `--stats/--history/--terms/--add-term`，WAL（G25）；**0.23 起术语相关 API 已删除**，现为 `--stats/--history` |
| 0.15 规则层（术语表 + 复读） | `spike/xifeng/glossary.py` | CER 从 3.3% → 0.0%（5/5 全对），无误伤 | ✅ 反例来自 `tests/test_glossary.py`（**0.23 起已下线**：`glossary.py`/`terms` 表/相关测试全部移除，纠错改由本地模型承担） |
| 0.16 首尾静音裁剪 | `spike/xifeng/vad.py` | 长静音不再触发幻觉，ASR 只跑语音段 | ✅ 15.6s（含 11s 静音）→ 裁 12.1s，ASR 1191→283ms；带裁剪重跑 CER **0.0%（5/5）** |
| 0.17 托盘 + 开机自启 + 无窗口 | `xifeng/{tray,autostart,applog}.py` | 常驻托盘、菜单开窗口/自启/退出、`--times 0` 无限 PTT | ✅ 冻结 exe 实测：控制台隐藏（`hwnd=0`）、日志落 `%LOCALAPPDATA%\Xifeng\xifeng.log`、`--autostart on/off` 写入/删除 HKCU Run |
| 0.18 GUI（M3） | `xifeng/gui.py` | 常驻托盘另起进程开窗口；四页：统计 / 模型 / 配置 / 历史（术语表并入配置页）。统计页有 P50/P95、输入字数、识别次数、超时次数（e2e>5s）与运行状态（CPU/内存/GPU占用/推理线程/当前模型）；历史页有三级对照（原文→清理→纠错）与“已纠错/全部”筛选；配置页有热键、录音段切分（原「触发模式」，实际是在按住说话的录音段内做 VAD 切分，非免按键）、语气词清理、注入方式、句末标点、中英文间距、首尾静音裁剪、模型下载源、ASR 推理线程、开机自启、本地数据目录 | ✅ tkinter 独立进程（`--gui --tab ...`）；冻结 exe 读真实库截图验证（2026-10-29） |
| 0.19 剪贴板兜底注入 | `xifeng/inject.py` | 不吃 `WM_CHAR` 的应用也能出字；剪贴板必须还回去 | ✅ `--inject auto|unicode|clipboard`；跨进程 WinForms `Edit` 实测：粘贴落地、探针识别、剪贴板恢复原内容（2026-10-29） |
| 0.20 深/浅主题 + UI 修复批次 | `xifeng/{ui,gui}.py`、`store.py` v3 | 双主题保存即生效；历史页带数据可渲染 | ✅ 设计令牌收进 `ui.Theme`（dark/light），`App.apply_theme` 原地重建 + DWM 标题栏；修复：`Page.row` 控件 z 序（G29A）、`self.dl/threads` 未挂、`Input` 占位符污染 var（G29C）、模型页 refresh 累积、history 缺 `fixes` 列（store v3 迁移）；视觉回归工具 `spike/ui_shots.py`（G30），临时产物进 `<仓库根>/temp/`（2026-10-30） |
| 0.21 Web GUI（vite+react+ts+shadcn/tailwind） | `spike/frontend/`、`xifeng/webgui.py`、`run_spike.py --web` | 五页 × 双主题截图自检通过 | ✅ 宿主 = 标准库 HTTP（仅 127.0.0.1:8642）+ Edge `--app` 窗口；pywebview 方案废弃（pythonnet 在本机加载失败，见 G31）；shadcn/ui 官方 CLI（new-york/zinc）+ 自定义 zinc 令牌；CDP 截图工具 `spike/cdp_shot.py`/`cdp_ask.py`（G31）；`build.py` 自动把 `frontend/dist` 复制为 `<exe 同级>/frontend`；五页（统计/模型/配置/历史/关于），统计页含结果分布/ASR 延迟/近 7 天趋势，主题经 `theme-color` meta 同步 Edge 标题栏（2026-10-30） |
| 0.22 按需开麦 + 交付界面切到 Web | `xifeng/{hotkey,popup,webgui,tray}.py`、`run_spike.py`、`frontend/` | 不按键时麦克风完全关闭；托盘菜单统一开 Web 五页 | ✅ `record_ptt` 改为等按下后 `LiveMic.start()`、松开 drain 后 `stop()/close()`（`--resident-mic` 把常驻保留为显式选项，见 G23）；浮窗 `bind()` 延迟到开录；`webgui.run(db, tab)` + 端口复用 + 30s 心跳看门狗；托盘菜单五页走 `--web --tab`，默认统计；侧栏 192px、卡片 8px 圆角、zinc 令牌对齐、7 天图柱宽限制；前端重建（2026-10-30）；冻结包 374.7MB 实测 `--web --tab about` 正常、关窗后 30s 服务自动退出 |
| 0.23 纠错改由本地模型（术语表下线） | `xifeng/correction.py`、`pipeline.py`、`settings.py`、`webgui.py`、`frontend/`、`packaging/build.py` | 默认关；开启后守卫必须拒绝删词/大改/解释性输出；配置页只剩一个「模型纠错」开关 | ✅ Qwen2.5-0.5B-Instruct Q4_K_M（491MB，外置 `models/llm`）+ llama-cpp-python CPU wheel；懒加载 + 启动 warmup（1.1s / 1.7s）；热身后 0.35–1.5s/句；实测 `帮我把…` 因 `drop-start` 回退、`三点` → `三点二十` 因 `edge-add` 回退、`red me` → `README` 通过；`glossary.py`/`terms` 表/CLI 术语命令/Web terms API 全部移除（旧库表保留不删）；`build.py` `--collect-all llama_cpp`；纠错模型**不进包**（放 `%LOCALAPPDATA%\Xifeng\models\llm`），交付包 392.2MB / 1294 文件，`fix_msvc_runtime()` 修掉冻结包 `llama_backend_init` 段错误（G33）（2026-10-30） |

| 0.24 录音浮窗（32px 毛玻璃条） | `xifeng/popup.py`、`run_spike.py`、`tests/test_popup.py` | 按住 Ctrl 时出现：左图标 + 右实时音柱，毛玻璃背景、圆角、主题跟随主界面、整体高度 32px | ✅ 300×32：左 22px 蓝底麦克风（与托盘图标同源）+ 右侧 30 根镜像音柱（`deque` 滚动历史 + 正弦包络 + 静音呼吸）；毛玻璃 = `show()` 时 BitBlt 抓背后 300×32（15–18ms）→ 4× 超采样高斯模糊 → 主题色叠加 → 缩回，圆角用 `-transparentcolor` 阈值化抠掉（DWM 亚克力对 Tk 无效，G34）；窗口只建一次复用（`close()` 只 `withdraw()`，第二次起 30–50ms 出现，首次含 Tk 初始化 ~330ms）；`theme` 每次按键前从 SQLite 重读；截图失败/无 Pillow 静默退回半透明纯色面板；新增 8 个单测（219 passed）（2026-10-30） |

**证明**：`cd spike && uv run pytest`（**219 passed**）+ `uv run python run_spike.py --help` 可运行 +
`packaging/build.py` 产出 `%LOCALAPPDATA%\Xifeng-build\dist\Xifeng\Xifeng.exe` +
`uv run python quality_check.py --wav-dir fixtures/quality` 平均 CER 0.0%。

---

## Step 0.21 · M1：Silero VAD 双阈值切分（2026-10-30 起）

Spec 依据：`spec.md` §9 M1 = "VAD 双阈值切分 + 无效段过滤 + 段重组"。
现状：`vad.py` 只有能量法（`segment()` 临时替身 + `trim()` 首尾裁剪，0.16）；
能量法对气声/极轻起音偏保守（见"仍待补"第 3 条）。

| 子步骤 | 产出 | 验证 | 状态 |
|---|---|---|---|
| 1 模型 | `models/vad/silero_vad.onnx`（629KB，GitHub Release） | sherpa VadModel 加载 + is_speech 出概率 | ✅ 真人 4.61s：语音检出 3.33s，边界干净 |
| 2 切分 | `vad.py` Silero 路径：`probs_to_segments` 双阈值滞回（0.5/0.35）+ `SileroVad` | 概率流单测 8 例 + fixture 切分冒烟 | ✅ |
| 3 无效段过滤 | 段长 < min_speech_ms 丢弃（G4 缓冲） | 短毛刺用例丢弃、相邻段合并/分离 | ✅ |
| 4 重组/回退 | `rebuild()` 不变；`cfg.use_vad` 走 Silero，模型缺失回退能量法 | `Pipeline._vad` + `load_vad()` 常驻单例 | ✅ |
| 5 质量回归 | golden 5 句 CER | **未过**：切分+重组 4.8%、Silero 裁剪 3.9–5.7%（基线 0.0%）；弱起音咬字 + 混排英文对边界敏感（见 latency-budget §3） | ⚠️ 决策：PTT 维持能量裁剪；Silero 留给免按键连续模式（逐段 ASR） |
| 6 延迟登记 | `latency-budget.md` §3 + `Trace.ms_vad` | 前向 1.2ms/窗（4.6s≈170–400ms） | ✅ |

**结论（2026-10-30）**：M1 的机制层（双阈值切分/无效段过滤/段重组/回退）完成并单测；
质量门槛显示 PTT 路径不应切换到 Silero（能量裁剪 0.0% 不可退化），Silero 目前只用于
"录音段内切分"（配置页「录音段切分」，默认关）。真正的免按键连续监听 **未实现**——托盘
主循环仍阻塞在 `record_ptt` 上；且它与 AGENTS.md「不录音时禁止占用麦克风」冲突，需先
明确例外再开工。bench 当日数字无效（机器 62% 后台负载，ASR 全局 3–4× 变慢，与代码无关；
热路径零改动），空闲时复测。

**风险**：Python 侧延迟不代表 Rust 侧；spike 数字只用于判断"模型是否可用"，不作为最终 SLO 依据。

**实测结果**：

| 日期 | 内容 | 数字 |
|---|---|---|
| 2026-10-28 | TTS 4.49s，8 线程 | ASR P50 329 / P95 355ms（RTF≈0.073），e2e P95 356ms |
| 2026-10-29 | 真人 5 句 CER | 3.3%（3/5 完全正确），配置 `language=zh` + 关 ITN |
| 2026-10-29 | 真人 4.61s，8 线程，CPU ~39% | ASR P50 512 / P95 539ms（RTF 0.111），e2e P95 582ms |
| 2026-10-29 | 同音频对照 `auto`+ITN | ASR P50 528ms —— 配置不影响速度，只影响准确率 |
| 2026-10-29 | PTT 时序自测（Medium + 自注入） | `ptt_selftest.py` 4/4；按住 700ms → 实测 702–708ms |
| 2026-10-29 | PTT 端到端（自注入 Ctrl，常驻流） | 按住 1.5s → 收 2.15s（含前置缓冲）；`--ptt --times 2` 2/2 Injected |
| 2026-10-29 | 规则层（术语表 + 复读折叠） | CER 3.3% → **0.0%**（5/5 全对），`ms_post` <1ms（0.23 起下线） |
| 2026-10-30 | 模型纠错（Qwen2.5-0.5B Q4，CPU 8 线程） | 加载 1107ms、warmup 1749ms；热身后 **350–1500ms/句**；守卫拒绝率实测（`帮我把…` / 句尾续写 / 补句号 全部回退） |
| 2026-10-30 | 冻结包（ASR 随包 392.2MB / 1294 文件，纠错模型不进包）纠错闭环 | `--wav` 纠错关 e2e 353ms；纠错开（warmup 1855ms + 纠错 693ms）e2e **1047ms**；`--text` `applied fixes=1 519ms`；修掉旧 `msvcp140` 导致的 `llama_backend_init` 段错误（G33） |
| 2026-10-30 | 浮窗 32px 毛玻璃条（源码形态渲染实测） | BitBlt 抓 300×32 **15–18ms**（`ImageGrab` 整屏 60–85ms）；显示耗时：首次 330ms（Tk 初始化）、复用后 **30–50ms**；`dark`/`light` 双主题截图核对（G34） |

数据见 `docs/01-overview/latency-budget.md`、`docs/04-reference/metrics.md`。

**已完成**：真麦克风采集（4.42s → ASR 275ms）、注入成功率（`inject_smoke.py` 5/5，chunk 32 与 8 均通过）、真人识别质量（CER 3.3%）、PTT 采集实现（钩子 + 轮询兜底 + 250ms 误触保护 + MAGIC 去自激）、录音链路（默认按下才开麦 + `drain_ms=120`；`--resident-mic` 可换常驻 + `pre_ms=300`，G23）、首尾静音裁剪、托盘/开机自启/Web GUI、剪贴板兜底。

**仍待补**：

1. **`SendInput` 跨进程真实延迟**：spike 的注入耗时是同进程 Tk 目标，只作上界参考。
2. **`auto` 的探测边界**：只有 Edit/RichEdit/Scintilla 这类能读文本的控件才敢自动兜底；
   控制台、全屏游戏（DirectInput）读不到，需要用户显式 `--inject clipboard`（见 `03-core-concepts/injection.md`）。
3. **静音裁剪的边界**：能量法对气声/极轻起音偏保守（省不到 10% 就不裁），
   极端样本仍可能把噪声当语音；正式版换 Silero VAD（M1）后再收紧。

---

## Step 1 · 工具链与骨架

工具链已落盘到 `D:\env`（最小集，C 盘不落组件，详见 `02-guides/rust-env.md`）：
rustc/cargo 1.99.1、MSVC 14.44.35207、SDK 10.0.22621.0、NSIS 3.11（`makensis` 可用）。
`just` 未装——纯 Rust 阶段先用 `cargo` 原生命令，真需要时再 `cargo install just --locked`。

产出：

```
voice-ime/
├── Cargo.toml                 # workspace
├── justfile                   # setup / check / run
├── rust-toolchain.toml
├── crates/
│   ├── ime-core/
│   ├── ime-audio/
│   ├── ime-infer/
│   ├── ime-post/
│   ├── ime-inject/
│   ├── ime-hotkey/
│   └── ime-service/
└── python/ime_infer/
```

**证明**：`cargo check --workspace` 通过（空实现）。

---

## Step 2 · ime-post（纯逻辑，先做）

理由：无外部依赖、可完全测试、是 spike 已验证规则的正式移植。

- 文件：`crates/ime-post/src/lib.rs`（单文件：词表 + 五步管线 + 单测）
- 词表：直接内联在 `lib.rs`（`HALLUCINATION` / `FILLERS_STANDARD` / `FILLERS_DEEP`），
  不再单列 `resources/` —— 词表与规则强耦合，拆文件只会多点同步成本
- 测试：`cargo test -p ime-post`（21 个，含 9 连阈值边界与 deep 档边界）

**状态：已落地（2026-11-09）**，并且是 **PyO3 接入的第一步**：

- `crates/ime-py` 把 `ime-post` 包成 `xifeng_rs` 扩展模块（`abi3-py311`）
- `spike/xifeng/postprocess.py` 保留纯 Python 基准实现，默认自动切到 Rust 后端
- `spike/tests/test_postprocess_parity.py` 用 40 条黄金用例 + 150 条随机串 × 9 组配置
  证明两边逐字节等价；没有扩展时自动 skip，纯 Python 兜底

**证明**：`cargo test -p ime-post` 21 绿；`uv run pytest` 236 绿；`postprocess.backend == "rust"`。

---

## Step 3 · ime-core（状态机 + Trace）

- `state.rs`：`Idle/Recording/Decoding/Processing/Injecting/{Cancelled,Dropped,Failed}`
- `trace.rs`：字段与 `spec.md` §5 一致
- `pipeline.rs`：依赖 `trait Infer`，测试用 `MockInfer`

**证明**：`cargo test -p ime-core` 覆盖每条状态转移与每种 outcome。

---

## Step 4 · ime-audio（cpal）

- 16k/mono/i16；若设备不支持 16k，则按设备默认率采集后重采样（rubato 或线性插值）
- 环形缓冲 + 无锁入队；回调内禁止分配/加锁/IO
- 录音上限 60s

**证明**：`cargo run -p ime-audio --example record -- 5` 生成 `out.wav`，时长与振幅合理。

---

## Step 5 · ime-infer（PyO3）

- `abi3-py311` + 内嵌解释器
- 推理线程唯一持有 GIL；音频线程永不触 Python
- `transcribe(&[i16]) -> Result<Transcript>`，超时 5s

**证明**：`cargo test -p ime-infer -- --ignored`（需模型）输出与 spike 一致。

---

## Step 6 · ime-inject（SendInput）

- 焦点快照 `{hwnd, pid, tid, class, elevated, is_console}`
- `Unicode` / `Clipboard` / `Auto`
- 提权检测、ALT 恢复技巧、代理对按码元发送

**证明**：`cargo run -p ime-inject --example inject -- "你好😀world"`，记事本读回一致。

---

## Step 7 · ime-service（串联）

- rdev 全局热键（Ctrl 长按，Esc 取消；左右 Ctrl 均识别，与 spike 的 `--key` 对齐）
- 串联 Step 2–6，写 `trace.jsonl`
- 单实例锁；`IME_MODELS_DIR` 支持

**证明**：`just run` 后完成 A0-1。

---

## Step 8 · 验收

按 `spec.md` §7 逐条跑，结果记入 `../08-acceptance/records/M0-<date>.md`。

---

## 偏离计划时的处理

实现与本文不符时，**在同一个 commit 更新本文件**。当前已知偏离：

| # | 偏离 | 原因 | 影响 |
|---|---|---|---|
| D1 | 先 Python spike 后 Rust | 本机无 Rust 工具链 | 多一步，但降低"设计假设全错"的风险 |
| D2 | M0 不接 VAD | spec §1 已排除 | 静音段依赖"按键起止"，噪声环境体验差 |
| D3 | ~~M0 不落 SQLite~~ → **已撤销（2026-10-29）** | 用户要求"有数据的地方用 SQLite"；历史/统计/术语天然是结构化数据，`sqlite3` 是标准库零依赖 | 新增 `store.py` + `03-core-concepts/storage.md`；DB 落 `%LOCALAPPDATA%\Xifeng`，Low 进程回退 `<spike>/.data`（G25） |
| D4 | Python 环境用 `uv` + 清华镜像 | pip 直连 pypi 单次安装 6 分钟未完成；uv 全量 17s | 环境为 `spike/.venv`（Python 3.11），与最终 embeddable 3.11 对齐 |
| D5 | 文档全部收进 `docs/`，根目录只留 `AGENTS.md` | 根目录文档会与代码抢注意力，且 `llms.txt` 已是入口 | 工件路径变深，引用需带 `00-process/` 前缀 |
| D6 | **交付形态改为 Python + PyInstaller，Rust 推迟**（2026-10-29 用户决策） | Rust 环境约 6GB（MSVC+rustup）；实测 ASR 占 512ms、宿主语言只差 10–20ms，P95 582ms 远低于 1500ms 硬指标；打包纯 Python 即可交付 | Step 1–8（Rust）从"M0 主线"降级为"M2 之后再评估"；`spike/` 由验证物升级为交付主程序；新增 `packaging/build.py` 与 `02-guides/packaging.md`；M4 的 python embeddable 方案由 PyInstaller 取代 |
| D7 | **托盘/无窗口用单份 console 产物 + `--tray` 隐藏控制台**（2026-10-29；GUI 部分 2026-10-30 更新） | 打两份 145MB 产物不值得；`ShowWindow(GetConsoleWindow(), SW_HIDE)` 即可，stdout 同时重定向到 `%LOCALAPPDATA%\Xifeng\xifeng.log` | 主界面用 **Web 独立进程**（`--web`，vite+React+shadcn 五页 + Edge `--app`，0.21/0.22），`--gui` 的 tkinter 四页仅作兜底；托盘用 `pystray`；自启写 `HKCU\...\Run`（带 `--tray`）；命名 Mutex 防双实例 |
| D8 | **首尾静音裁剪默认开启**（2026-10-29） | 物理键实测 16.6s 只说了开头，后半段静音被解码成 `喂喂喂…`；ASR 1191ms 有大半花在静音上 | 能量法保守裁剪（峰值 5% / 噪声 3× / 绝对 200 取大；省不到 10% 不裁），5 句真实音频带裁剪重跑 CER 仍 0.0%；`--no-trim` 可关 |
| D9 | **麦克风改为按需开麦**（2026-10-30 用户决策，修订 G23 默认） | 常驻流让麦克风指示灯常亮、一直占用设备；实测代价是按下后 100–400ms 开流静默期，句首可能丢字（按住后先说「嗯」确认再说话可避开）。常驻模式保留为 `--resident-mic` 显式选项 | 取消「常驻流」为默认；`LiveMic` 每次录音新建、结束即关 |
| D10 | **PTT 录音指示浮窗 + 组合键中止**（2026-10-30） | 用户要求：按住说话时有可视化反馈；Ctrl+其它键的组合不得触发语音 | `xifeng/popup.py`（Tk 浮窗，按需开麦下 on_start 时绑定音量源）；`PttResult.combo`：按住期间任何其它键落下 → 整段丢弃（Esc 除外，仍走取消） |
| D11 | **PTT 触发改为双击后保持长按**（2026-10-31 用户决策，取代 D10 的组合键中止为主防线） | 单击触发下 Ctrl 组合键依赖运行期检测，浮窗仍会闪现；双击天然过滤所有单击组合键 | `record_ptt(trigger="double")`：单击 ≤300ms → 500ms 内再次按下并保持 → 录音；组合键中止保留为录音中的第二道防线；`--single-press` 可回退旧方式；浮窗在录音开始时立即显示（不再需要说话声检测/延迟） |
| D12 | **L1 模型纠错上线（M2 首版）**（2026-10-31） | 用户要求纠错覆盖整句优化（删多余/补缺失） | `xifeng/correction.py`（Qwen2.5-0.5B Q4 + llama-cpp-python 0.3.36 CPU wheel，llama-server 方案废弃：v0.8.0 无 Windows 二进制）；few-shot 提示词 + 守卫拒绝回退 L0；默认关（配置页「模型纠错」开关）。与并行实现的 correct.py 去重：保留 correction.py | 实测 293–905ms/句（≤30 字）；0.5B 质量边际：纯指令跑偏成翻译（few-shot 修复），术语表进 prompt 会稀释示例（术语由 L0 先行替换） |
| D13 | **Rust 以 PyO3 扩展参与，Python 保持主流程**（2026-11-09 用户决策，修订 D6） | D6 的「Rust 不重写主流程」仍成立——但把规则与 Win32 这类易静默出错的逻辑沉进 Rust 更稳，且不必承担 ASR/GUI 的移植成本。第 1 阶段实测：延迟几乎不变（postprocess <1ms、inject 10–39ms），收益在类型安全与可测性 | 新增 `crates/{ime-post,ime-py}`，产物 `xifeng_rs.pyd`（abi3-py311）；`postprocess` 默认走 Rust，纯 Python 保留为**行为基准 + 无扩展时兜底**，由 `tests/test_postprocess_parity.py` 逐字节校验；第 2 阶段**已搬 VAD**（crates/ime-vad，2026-11-09，release 下切分快 6.8×，20 个 parity 用例）；hotkey/audio/inject 待搬 |
| D14 | **交付形态改为 Tauri 壳 + Python 子进程（方案 A）**（2026-11-09 用户决策，取代 D6 的 PyInstaller onedir） | 用户要求"转 tauri+前端+pyo3"。三选一（A 子进程 / B 内嵌解释器 / C 全重写 Rust）中选 A：前端 React 与 Python 核心零改动，风险最低、最快见效；代价是分发仍带 Python（约 60–80MB），但相比 PyInstaller 145MB 与 Edge `--app` 依赖仍有收益 | 新增 `spike/src-tauri/`（Tauri 2 窗口 + 子进程管理）；Python 侧加 `--ipc` 走 stdio NDJSON；契约见 `03-core-concepts/ipc-contract.md`（14 个 method 与 `webgui.Api` 一一对应）；前端 `api.ts` 按 `window.__TAURI__` 自动切换传输层，页面组件零改动；HTTP 模式保留供 `--tray` 与调试。**PyInstaller 不删**，作为 Python 核心的便携打包器并存 |
| D15 | **模型统一根：`%LOCALAPPDATA%\Xifeng\models\{asr,vad,llm}`**（2026-11-11 用户决策） | 模型原来分散两处（ASR/VAD 随 exe 或仓库根，LLM 在数据目录），打包/便携/开发各认各的；统一后开发调试与交付共用一处，仓库不再放模型 | `paths.models_root()`：`IME_MODELS_ROOT` → LOCALAPPDATA 只读探测（Low 进程写不进但读得到，G25）→ `data_dir()/models`；旧布局（exe 同级/仓库根）仅作兼容兜底；**安装器默认不带模型**（2026-11-11 用户决策，运行时走统一根；`build_core.py --with-models` 可打离线开箱包）；仓库 `models/` 已物理移除，`build.py` 复制源改统一根 |
| D16 | **Rust+前端为主技术栈，Python 降为模型运行时**（2026-11-13 用户决策，修订 D13/D14 分工） | D14 迁 Tauri 后触发链（热键/托盘/浮窗）仍留在 Python `--ptt --tray` 模式里，交付形态没人拉起该进程 → 装上即没语音（结构性遗漏）；且 Python 常驻进程带全套 GUI 职责不利于壳统一 | 壳（Rust）接管：WH_KEYBOARD_LL 热键、cpal 采集、ime-vad 裁剪、ime-post 后处理、SendInput 注入、Tauri 托盘（两项：显示主界面/退出）、webview 浮窗；Python sidecar 只做 ASR/纠错推理（IPC：`asr_begin/asr_feed/asr_end/asr_cancel`，吃 PCM 吐文本）。过渡期壳先拉起 Python `--ptt --tray` 语音服务止血（7946cf3），P1 完成后退役 |
| D17 | **壳侧触发改回单击（按住即录），双击状态机删除**（2026-11-14 用户决策，取代 D11 在壳内的实现） | 用户明确要求「把双击改成单击」：双击要按两次才能开口，日常输入里多余且容易漏拍。D11 选双击的理由（过滤 Ctrl 组合键）已不必要——`Ctrl+C` 这类**单击组合**在按住即录下表现为 hold<250ms 的误触，本来就整段丢弃 | `voice.rs:wait_for_press`（按下即 `mic.start()` 并返回，松开/组合键/Esc/`MIN_HOLD_MS=250` 判定由 `run()` 录音循环接管）；删除 `TAP_MS`/`GAP_MS`/`wait_for_double_tap`/`Mic::wait_live`/`Ring::len`。浮窗随按下立即弹出（零等待）：cpal 实测 `play→首回调=17–24ms`，G23 的 95–376ms 静默期只属于旧 PortAudio 栈。**句首丢失的真因另有其人**（VAD 首段 padding，G39），不是开流 | e2e：单击按住两遍「你好喂，今天天气不错」→ `chars=9` × 2、`outcome=Injected`；`tap` 单点 → hold<250ms → `asr_cancel`；`cargo test`（壳）13 绿 |
| D18 | **热键左右互斥 + 组合键不触发**（2026-11-14 用户决策，修订 D17） | 用户实测两件事：①设置一侧后另一侧照样触发（旧值 `ctrl` 表示"左右均可"，等于设置失效）；②`Ctrl+C` 会闪一下浮窗。要求「设哪边就只认哪边」「含 Ctrl 的组合键不要触发」 | `KeyPreset` 只保留 `lctrl`/`rctrl`/`lshift`/`rshift`（默认**右 Ctrl**），旧值 `ctrl`→右、`shift`→左（`settings.load` 与 `KeyPreset::parse` 双端归一，老库不用重写）；`wait_for_press` 增加 `COMBO_GUARD_MS=150` 观察窗：按下后 150ms 内出现任何其它键（含按下前就按住的 Shift/Alt，`Hook::down_except`）→ 静默丢弃（不弹浮窗、不发 `asr_begin`、立刻收流等松开）；**开麦仍在按下瞬间**，观察窗内音频留在环形缓冲（feeder 在 `asr_begin` 后才启动），句首不受影响；按住时长改从按下瞬间计（含观察窗），否则 <250ms 误触会误伤正常短句 | `cargo test`（壳）15 绿：`presets_are_side_exclusive`、`parse_maps_legacy_values_to_one_side`；pytest：`test_hotkey_sides_are_exclusive`、`test_legacy_hotkey_value_normalized_to_one_side`；e2e：`Ctrl+字母` 不再弹浮窗（G40） |
| D19 | **麦克风不常驻、不预热：按下才开流**（2026-11-15 用户决策，给 D17 的"零等待"补上真实口径） | 用户问"浮窗是不是等麦克风授权才弹出"。分段计时证明**不是权限问题**（Windows 没有按应用的授权弹窗）：`build_input_stream`（WASAPI `IAudioClient::Initialize`）才是大头，`play()`=0ms、`show()`=0–1ms。更关键的是它与空闲时长强相关：**空闲 ≤4s → 120–150ms；≥6s → 450–530ms；启动后第一次 → 805ms**（Windows 约 5s 就回收音频引擎），启动时预热一次也留不住（实测预热后 12s 再按仍是 805ms）。因此"按下即说"必然要付这笔钱或让流常驻 | 不改行为：仍按需开麦（松手即 `stop_stream`），浮窗在流真正起来后才弹（它如实表示"已在采"）；撤掉试做过的 `Mic::warmup()`（对日常使用无效，白占 0.7s 麦）。要给"最跟手"就得常驻麦（指示灯常亮），用户明确不选 | e2e（便携壳 + SendInput）：空闲 ≤4s 时按下→浮窗 161–192ms；空闲 ≥6s 时 454–533ms；启动后第一次 911ms。G23 补注"play→首回调只覆盖建流之后"，另见 G41 |
| D20 | **浮窗与开流并行**（2026-11-16 用户决策，修订 D19/G41 的"弹窗=已在采"口径） | 用户问"就不能按下 ctrl，弹窗和拉麦克风同时开启？"。查代码发现是**串行**：`wait_for_press` 先阻塞 `mic.start()`（冷态 450–800ms）再 `show()`——物理成本（G41）被实现顺序直接摆到用户面前。`cpal::Stream` 是 `!Send`，不能把开流挪到别的线程，能动的是弹窗那一路 | 按下即起"弹窗线程"：等满 `COMBO_GUARD_MS` 后自判组合键/是否仍按住，通过就 `show()`；主线程同时 `mic.start()`；`popup.join()` 回传 `(已弹窗, 组合键)`——`drain_presses` 是取走语义，不回传会让主线程漏判组合键（G40 防线静默失效）。组合键仍由主线程在"已弹窗"时补 `hide()` | e2e（便携壳 + SendInput）：启动后第一次 911→**211ms**、空闲 15s 510→**168ms**、热态 161–192→**177ms**；组合键三例（右Ctrl+F24 / 先左Shift再右Ctrl / 左Ctrl）2ms 级盯窗**零闪窗、零会话**。代价：冷态浮窗早于真实采集 300–650ms（G41 的物理限制仍在，D19 仍否决常驻麦） |
| D21 | **全量改名 VoiceIME → Xifeng（溪风）+ 数据迁移**（2026-11-16 用户决策） | 用户要给项目起英文名，要求带「溪风」的意思或谐音，并选「全量改名 + 数据迁移」。最终定 **Xifeng**（谐音溪风；备选 Rillwind/Brookbreeze 已评估）。改名若只改显示名，老用户的 `%LOCALAPPDATA%\VoiceIME` 历史/设置/228MB 模型会凭空消失，注册表旧自启值还会让开机跑两个实例 | 映射：产品名/窗口/托盘/安装器 → Xifeng；壳 exe → `xifeng.exe`；sidecar → `Xifeng-Core`；identifier → `com.xifeng.app`；数据目录 → `%LOCALAPPDATA%\Xifeng`；库/日志 → `xifeng.db`/`xifeng.log`；自启值 → `Run\Xifeng`（写新删旧）；Python 包/PyO3 模块 → `xifeng`/`xifeng_rs`；环境变量 → `XIFENG_*`（壳对 `VOICEIME_*` 保留兼容读取）。迁移在 `run_spike.main()` 显式调用`paths.migrate_legacy_data()`：整目录 rename 优先（同卷瞬时）、空壳先删、已有数据只补缺不覆盖、库/日志连 `-wal`/`-shm` 一起改名、失败只告警且 `db_path()`/`models_root()` 回退旧目录。`IME_DATA_DIR` 显式指定时完全不动。安装器与便携包落到新名目录（`D:\env\xifeng-portable`） | 证明：`tests/test_paths.py` 新增 6 个迁移用例（整目录接管/空壳/合并不覆盖/显式覆盖跳过/失败回退库/失败回退模型）；`cargo test` 壳 15 绿、`uv run pytest` 全绿；便携壳冷启动把真实旧目录（含模型）迁到 Xifeng 后历史与模型仍在；e2e 右 Ctrl 注入、Ctrl+其它键不触发、浮窗 ~150ms 与改名前一致；2026-11-17 在真实机器上冷启动便携壳，旧目录 221 条历史 / 16 项设置 / 698MB 模型整目录 rename 到 `Xifeng`，旧目录未再生成；同次发现 sidecar 迁移日志按 gbk 写进壳的 UTF-8 日志管道会断流，已在 `main()` 里按 `--ipc` 提前 reconfigure；安装器用 `nsis/hooks.nsh` 把默认落点从 `%LOCALAPPDATA%\Xifeng`（= 用户数据目录）挪到 `%LOCALAPPDATA%\Programs\Xifeng`，实测卸载/重装后 221 条历史与 698MB 模型仍只在数据目录 |
| D22 | **公开发版：仓库 public + Release v0.1.0，检查更新读 GitHub latest**（2026-11-17 用户决策） | 「关于」页的 GitHub 按钮与「检查更新」都指向 `Topskys/Xifeng`；应用内检查更新是**匿名**请求 `api.github.com/repos/Topskys/Xifeng/releases/latest`，private 仓库对匿名一律 404——改代码修不好，只能公开仓库并发 Release | 仓库转 public，打 tag `v0.1.0`，Release 附 `Xifeng_0.1.0_x64-setup.exe`（34.8MB）与 `Xifeng_0.1.0_x64-portable.zip`（50.7MB）；`REPO` 常量统一为 `Topskys/Xifeng`（About 按钮同源）；版本比较改用 `version_key()` 按数字段（`0.10.0 > 0.9.0`，`1.0 == 1.0.0`）；404 返回「仓库暂无 Release（或仓库未公开）」而不是裸异常；根目录新增 `README.md` | 证明：匿名 API 返回 `tag=v0.1.0`、2 个资产；应用内 `check_update` → `{'latest':'0.1.0','current':'0.1.0','update':False}`；`pytest` 330 绿（新增 `version_key` 与 404 分支 2 个用例）；README 6 个相对链接全部存在；发版步骤写进 `02-guides/packaging.md` |


---

## D16-P1 实施计划（2026-11-13/14，壳接管触发链）

> 本节是 D16 的 G1/G2 工件：接口契约、文件清单、顺序、风险、证明方式。
> **跨边界契约必须写在这里**（不活在对话/代码注释里），改契约先改本节。
>
> ⚠ 本节记录 2026-11-13/14 的 P1 计划原文。触发方式后来被 **D17（单击按住）**
> 与 **D18（左右互斥 + 150ms 观察窗）** 取代，下文时序里出现"双击"的段落以 D17 为准。

### 接口契约（跨边界）

| 边界 | 契约 | 落点 |
|---|---|---|
| Rust 壳 ↔ Python 运行时 | `asr_begin{}` / `asr_feed{pcm_b64}`（16k 单声道 i16 LE base64，60s 上限，超出丢弃新数据）/ `asr_end{}` → `{text,chars,ms}`（**原始文本，不做后处理**）/ `asr_cancel{}` | `docs/03-core-concepts/ipc-contract.md`（P1a 已实现） |
| Rust 注入模块 API（inject.rs，inject.py 等价移植） | `EXTRA_INFO_MAGIC: usize = 0x5649_4D45`；`snapshot_focus() -> Option<FocusSnapshot{hwnd,pid,class_name,elevated:Option<bool>,is_console}>`；`build_events(text) -> Vec<Event{Text(u16),Key(u16)}>`；`probe_focus_text(limit) -> Option<FocusText{length,text:Option}>`；`definitely_unchanged(before,after)`；`inject_ex(text, snap, cfg) -> InjectResult{outcome,strategy}`；`InjectStrategy::{Unicode,Clipboard,Auto}::parse` | `spike/frontend/src-tauri/src/inject.rs`（分支语义 MUST 逐条对齐 inject.py：G2/G3/G6/G7/G13/G20） |
| Rust 钩子 API（hotkey.rs） | `Hook::start() -> Arc<Hook>`（失败返回 active=false 实例 + `error()` 码）；`pressed(vks)` / `drain_presses() -> Vec<u32>`（不漏极短敲击）/ `event_count()` / `injected_count()` / `stop()`；回调只忽略带 MAGIC 的自家注入（G20） | `spike/frontend/src-tauri/src/hotkey.rs` |
| 壳 ↔ 浮窗页 | 浮窗 URL `index.html?window=overlay`；事件 `voice://level`（f32 RMS 0..1，~40ms 一发，仅录音中）；窗口生命周期由编排器 show/hide，页面无状态 | `voice.rs` ↔ `src/pages/Overlay.tsx` |
| 壳 ↔ 前端（自启） | Tauri 环境自启走 Rust 命令 `autostart_cmd({enabled?:bool})`（HTTP 开发模式仍走 Python `autostart` 方法）；注册表值名统一 `Xifeng`（HKCU\...\Run），目标=壳 exe，Python bootstrap 的 `autostart` 布尔读同一键 → 状态互通 | `autostart.rs` ↔ `api.ts` |
| 托盘 | 两项：显示主界面 / 退出；主窗口 × = 隐藏到托盘不退出（`api.exit(0)` 才真正退，Exit 挂 `bridge.shutdown()`） | `tray.rs` |
| 采集 | cpal 按设备**原生格式**开流，喂送线程下混+线性重采样 → 16k；音频回调只做「转 i16 → 拷进无锁 SPSC 环形缓冲（AtomicU16 游标）」，禁止 IO/锁/分配大对象；满则丢弃计数 | `voice.rs`（硬约束，同 Python 版） |

### 时序语义（与 hotkey.py 逐条对齐）

双击触发（单击 ≤300ms 松开 → 500ms 内再按并长按）→ `asr_begin` → 开麦 → 焦点快照 → 录音循环（Esc 取消 > 组合键整段丢弃 > 松开 > 60s 截断，轮询 10ms）→ 松手收 drain 120ms 尾音 → 关流 → 误触判定（<250ms 且非取消 → `asr_cancel`）→ `asr_end` → ime-post → 注入（auto 策略）。空文本/后处理判丢不注入；日志禁落识别全文。

### 顺序与证明

| 步骤 | 内容 | 证明 |
|---|---|---|
| P1a（已完成 afc3e2b） | IPC 运行时方法 | pytest 309 全绿 + core_smoke + e2e 脚本 |
| P1b+P1c（已完成，本 commit） | hotkey.rs / voice.rs / inject.rs + main.rs 接线 | `cargo test` 13 单测零 warning ✓；便携壳 e2e（`tools/voice_e2e.py` 经 CDP 9334 驱动真实钩子）：双击触发 ✓ / 误触（hold<250ms 丢弃+asr_cancel）✓ / 正常会话（hold=749ms→asr_end in_frames=8481 ms=67→空文本不注入）✓ / 单击对照组不触发 ✓ / 注入 smoke（voice_test inject）outcome=Injected 且自家钩子零误触发（G20）✓ |
| **延迟报数（G3，已完成）** | 各阶段耗时落日志：`会话完成：hold=…ms 松手→asr_end=…ms 后处理=…ms 纠错=…ms 注入=…ms`；asr 解码 67ms/0.53s 音频，松手→asr_end 22–68ms（+drain 120ms） | 实测对预算余量充足；asr_feed IPC 往返成本单测（P2-① 补）：**真实子进程管道逐条计时 avg=0.26ms / p50=0.25ms / p95=0.31ms**（20 批 × 640 帧），远低于 40ms 窗口预算 |
| **重采样质量（G3，已完成）** | 16k 真人录音 → 高质量上采 48k（模拟 WASAPI 原生流）→ Rust Resampler 等价线性插值降采 16k | 基线 CER 3.3%（正确 3/5） vs 重采样路径 **CER 2.4%（正确 4/5）**，不劣化，合格（`tools/resample_cer.py` 可复现） |
| P1d（已完成，本 commit） | tray.rs / autostart.rs / Overlay.tsx / withGlobalTauri / 退役 spawn_voice_service | 前端 build ✓；便携壳实测：托盘两项 / 自启写键（值名 Xifeng 指向壳 exe，与 Python 状态互通）/ 浮窗 webview（黑 Mic 图标）show-hide ✓ |
| 合并前（已完成） | 独立上下文复审（新会话只给 diff + 本节 + review 清单，21 项逐条核） | 复审结论：inject.rs/hotkey/voice 语义对齐全部通过；**阻断项 1（用户设置被默认值覆盖）已修**（post_cfg/inject_cfg 传入 process/inject_ex）；建议项已处理：环容量按原生率×声道重算 ✓、dropped/流错误计数接出日志 ✓、未知注入策略边界显式报错 ✓、autostart_cmd 统一 {ok,error} ✓、编排线程 spawn 失败降级不 panic ✓、AGENTS.md 口径更新 ✓；其余登记见风险节 |

### 复审裁决记录（存疑项定夺）

- **部分成功 SendInput 报成功**：与 Python 基线行为一致（inject.py:556-557 同款），非回归，保持现状。
- **~~G7 提权目标无 UI 反馈~~（P2-③ 已接）**：壳 emit `voice://elevated`（ElevatedTarget 时）→ 主窗口 App.tsx 监听 → toast 提示；与统计/历史反馈链同批闭环。
- **voice://level 节奏**：契约改为「喂送循环每轮一发（仅录音中）」，与实现一致；固定 40ms 节流随 P2 喂送节流做。
- **未知注入策略**：settings 边界显式 ERROR 日志 + 按 auto 继续（宁可功能可用，错误显眼；不做静默）。

### 风险登记

- **重采样 CER**：已过 CER 对比（2.4% vs 基线 3.3%，不劣化），关。算法单测对照脚本 `tools/resample_cer.py` 留档。
- **settings 热更（P2-① 已接）**：`voice_reload_settings` 命令在前端保存设置后触发
  （api.ts settings_save/settings_reset 成功后 invoke），重新 load 写进共享
  `Arc<Mutex<VoiceSettings>>`；编排循环每次**会话开始**取一次——改动在下一次语音会话生效
  （会话中段换配置没有意义）。CDP 实测：命令 OK + 壳日志「设置已热更」。
  喂送节流同步完成：固定 40ms 窗口，批大小不再随 IPC 速度漂移。
- **~~纠错（LLM）暂不在 Rust 链路~~（P2-② 已接）**：壳在 asr_end→后处理后按 `model_correct` 调 IPC `correct`
  （applied=true 才采用修正文本，重新 normalize+final_check，与 pipeline.py 同序）；G32 守卫在 Python `correction.check` 内；
  LLM 冷启动由 Python warmup 预热（`correct 模型已热身`）。实测真实进程：预热 1832ms、推理 588ms、守卫拒绝返回原文。
- **~~统计/历史断档~~（P2-② 已接）**：注入后壳调 IPC `session_record` 写 history（mode=ptt），
  store_text 关闭时三级对照都不落文本（开关在 add_history 内生效）；记账失败恒 ok=true 不影响注入。
- **钩子装不上**（Low 完整性目录/安全软件）：壳降级为"窗口可用无语音"并打日志（G21/G24），安装器产物在仓库外构建规避。
- **~~G7 提权目标无 UI 反馈~~（P2-③ 已接）**：注入 outcome=ElevatedTarget 时壳 emit
  `voice://elevated`，主窗口 App.tsx 监听后 toast 提示（提权窗口收不到语音输入）。
- **~~voice_test 后门~~（P2-③ 已门控）**：`#[cfg(any(debug_assertions, feature = "e2e"))]`——
  正式 release 产物里 voice_test/send_ctrl/test_inject 不编译，CDP 调用显式报错；
  便携 e2e 壳用 `cargo build --release --features e2e`。实测：plain release 报
  「voice_test 仅在 debug 或 e2e 构建可用」，e2e 壳全链路回归通过。
- **~~老自启键迁移~~（P2-③ 已做）**：壳启动时 `autostart::migrate_legacy()`——Run 值
  `Xifeng` 存在且目标 ≠ 壳 exe 即覆写为壳 exe（保留自启意图），旧值落日志；
  没开自启/已是壳 exe 则无事可做。实测：写 Python 时代假旧值 → 起壳 → 覆写
  `"...xifeng.exe"` + 日志「已迁移旧版自启项」；重启无重复迁移。

---

## 验证命令（一条命令原则）

```bash
just check                     # fmt + clippy + test（spike + rust）
just run                       # 启动 ime-service
cd spike && uv run python bench.py --n 20   # 输出 P50/P95
```
