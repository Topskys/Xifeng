# AGENTS.md — Xifeng

本地语音输入法。**Python 主流程**（sounddevice + ctypes Win32）+ SenseVoice ASR，
PyInstaller 打包交付。Rust 版推迟（决策见 `docs/00-process/plan.md` 偏离 D6）。
**端到端 P95 ≤ 1500ms 是硬指标**（实测 P95 582ms）。

## 命令

```bash
# Python 命令在 core/ 下执行（uv 管理环境，Python 3.11，镜像已配清华源）
cd core
uv sync                                  # 首次/依赖变更后
uv run pytest                            # 单测（361 个，约 45s）
uv run python tools/build_ext.py --release # 编译 Rust 扩展（debug 比 numpy 慢，默认用 release）
uv run python run_spike.py --help        # 手动跑一次
uv run python bench.py --n 20            # 输出各阶段 P50/P95（自动判环境档位）
uv run python quality_check.py --wav-dir fixtures/quality   # 复算 CER
uv run python ptt_selftest.py            # PTT 时序自测（需 Medium 完整性）
uv run python run_spike.py --web         # 打开 Web 主界面（五页）
uv run python run_spike.py --tray        # 托盘常驻（默认按需开麦）
uv run python run_spike.py --language en # 指定识别语言（默认读配置）

# 打包（MUST 用仓库外的 Medium 解释器，见 docs/02-guides/packaging.md）
%TEMP%\vi-venv\Scripts\python.exe packaging\build.py

# Tauri 内嵌核心 sidecar（见 docs/02-guides/packaging.md「sidecar」节）
uv run python packaging/build_core.py    # 产出 apps/desktop/src-tauri/resources/core/（不进仓库）
uv run python tools/core_smoke.py        # 验证 sidecar 能独立跑 IPC（ready→bootstrap→shutdown）
uv run python tools/ask_boot.py 9334     # 从 Tauri 窗口经 CDP 验证 py_call（需先起壳+调试端口）

# 前端 / 壳（pnpm workspace，命令都在仓库根执行）
pnpm install                             # 首次/依赖变更后（apps/* 一并装）
pnpm dev                                 # 仅前端（浏览器 + Python --web）
pnpm build                               # 前端产物 apps/desktop/dist
pnpm tauri:dev                           # 壳内跑（Rust + WebView2）
pnpm tauri:build                         # 出安装包

# Rust（crates/，上机位置见 docs/02-guides/rust-env.md）
cargo test -p ime-post                   # 后处理规则单测（21 个）
cargo test -p ime-vad                    # VAD 算法单测（15 个）
cargo build -p ime-py --release          # 只编译扩展，不装进 venv
```

> Python 环境 MUST 用 `uv`（`core/pyproject.toml` + `core/.venv`）。
> JS/TS 依赖 MUST 用 `pnpm`（`pnpm-workspace.yaml`，根 lockfile）；禁止 `npm install`。
> 禁止 `pip install` 到系统 Python；禁止提交 `.venv`、`node_modules` 与 `__pycache__`。
> 本仓库目录带 **Low 完整性标签**：从这里启动的 exe 收不到硬件按键（G21），
> 打包产物 MUST 落在仓库外的目录（G24）。

## 硬约束（违反即打回）

- 音频回调里 **禁止** IO、日志、推理、加锁；只允许"拷贝一块 → 入队/入环形缓冲"
- 音频/文本 **禁止** 离开本机；联网只有模型下载与版本检查（后者只取 release 元数据）
- 注入前 MUST 校验焦点仍在快照窗口；提权目标 MUST 显式失败，禁止静默
- 换行用 `VK_RETURN`，**禁止** 发送 Unicode `\n`
- 注入文本 MUST 经过 `postprocess`；开启纠错时模型输出 MUST 通过守卫（删词/大改/解释性输出一律丢弃）；空结果不注入
- 剪贴板兜底 MUST 恢复原内容；若期间剪贴板已被改写则 MUST NOT 覆盖
- 日志禁止记录识别文本全文，只记长度与 outcome
- 推理 MUST 在独立线程，**禁止** 在音频回调里调用 ASR
- 不录音时 **禁止** 占用麦克风：默认按下热键才 `LiveMic.start()`，松开后关闭；
  常驻只是用户显式开启的选项（设置「常驻麦克风」/ `resident_mic`，默认关，D24）
- 后处理（`postprocess`）规则 **禁止** 只改一边：`xifeng/postprocess.py` 是基准，
  `crates/ime-post` 是等价实现，`tests/test_postprocess_parity.py` 必须绿
- 用户数据 MUST 落 `%LOCALAPPDATA%\Xifeng`（库 `xifeng.db`、日志 `xifeng.log`、
  **配置 `config.json`**）；旧 `VoiceIME` 目录只允许**一次性迁移 + 只读兜底**，
  禁止把新数据写回去（D21/G43）
- 自启 MUST 只写 `HKCU\...\Run\Xifeng`，旧值 `VoiceIME` 迁移后 MUST 删除
  （否则开机跑两个实例）；环境变量用 `XIFENG_*`，旧名只读兼容
- 设置 MUST 存 `config.json`（外置 JSON，用户要能看懂并手改）；**禁止**再写
  SQLite `settings` 表（只读用于一次性迁移）。写配置 MUST 原子（临时文件 + replace），
  坏文件 MUST 备份而不是让程序起不来
- 识别语言 MUST 显式指定（zh/en/ja/ko/yue）：`auto` 会把普通话判成粤语（G22）；
  换语言 MUST 重建 recognizer（language 是 sherpa 的构造参数），MUST 经 IPC
  `set_language`，禁止静默忽略非法值

## 架构

```
Tauri 壳(Rust): hotkey(WH_KEYBOARD_LL) → voice(编排: cpal 按需开麦→SPSC 环形缓冲→喂送线程)
        → asr IPC(SenseVoice, Python 运行时) → post(ime-post Rust) → inject(SendInput)
        托盘/自启/浮窗(webview)/设置读取 都在壳里
Python: 仅 ASR/纠错推理运行时（--ipc stdio NDJSON，asr_begin/feed/end/cancel）
```

D16 后：触发链在壳里，**Python --ptt --tray 语音服务已退役**（spawn_voice_service 已删）；
托盘两项（显示主界面/退出）、开机自启写注册表值 `Xifeng`（HKCU\...\Run，指向壳 exe）、
录音浮窗是 `?window=overlay` webview 页（`voice://level` 音量事件）。
已知限制：设置热更分两类（D24）——**构造参数**（语言/线程数/模型目录）在保存时
经 `AsrRuntime.apply_settings` 立即重建 recognizer；**其余项**在下一次语音会话
边界生效（会话中段不变）；纠错（LLM）与
统计/历史已走 IPC（correct/session_record）；G7 提权目标已接反馈链
（voice://elevated → 主窗口 toast）；voice_test 仅 debug/e2e 构建可用。

Rust 以 PyO3 扩展（`xifeng_rs.pyd`）参与：`crates/ime-post` 放文本规则，`crates/ime-vad` 放 VAD 算法，`crates/ime-py` 只做
Python 签名 ↔ Rust 的转接。Python 侧保留纯 Python 基准实现，两边必须逐字节等价。

## 提交

**每完成一个阶段 MUST 提交一次**（一个阶段 = 一个可验证的闭环：能跑测试、能证明目标达成）。
提交前跑过该阶段的门禁命令（`uv run pytest`、`cargo test` 等），提交信息按 Conventional Commits，
正文写清「做了什么 / 怎么证明」。阶段内的小步可以攒，阶段结束不许留未提交改动。

## 常见错误（改这里之前先看）

- 不要用 `str` 逐字符注入 —— 必须 `encode("utf-16-le")`，否则 emoji 乱码
- 不要"按下才开流"却又想要零延迟 —— 旧 PortAudio 栈开流有 95–376ms 静默期，
  但壳侧 cpal **实测 play→首回调仅 17–24ms**（2026-11-14 复测），所以
  「按下即开流 + 立刻弹浮窗」本身就是零等待（G23）；想连这 20ms 抖动也消掉，
  只能让用户显式打开常驻麦克风（默认关，见 D24），**不要替所有人默认开启**
- 不要指望 VAD 只留 60ms padding —— Silero 起音判定有 100–250ms 延迟，
  首段 MUST 额外留 300ms，否则第一个音节的起音被切（「你好喂」→「好喂」，G39）
- 不要再提供"左右均可"的热键 —— 左右 MUST 互斥（默认右 Ctrl），旧值
  `ctrl`→右、`shift`→左；组合键（Ctrl+C）MUST 在 150ms 观察窗内静默丢弃，
  不许靠录音循环事后作废（那时浮窗/开麦/ASR 会话已经发生，G40）
- 不要把"浮窗慢"当成麦克风授权问题 —— Windows 没有按应用的授权弹窗，慢在
  `build_input_stream`（引擎空闲 >5s 就被回收：热 120–150ms、冷 450–800ms，G41）
- 弹窗 MUST 与开流**并行** —— 观察窗一过就 `show()`，禁止串行等 `mic.start()`
  返回（那是把 450–800ms 摆在用户面前）；`popup.join()` 回传的"组合键"判定
  必须采纳，`drain_presses` 是取走语义，不回传会让 G40 的防线静默失效（G42）
- 不要松手立刻关流 —— 会丢最后 150–300ms，必须 `drain_ms`（G23）
- 不要在托盘菜单里起 tkinter —— 主界面是 Web（`--web --tab`），`--gui` 只是旧版兜底
- 不要按次加载模型 —— 模型常驻，启动 warmup 一次
- 不要把模型放进仓库 —— 统一根是 `%LOCALAPPDATA%\Xifeng\models\{asr,vad,llm}`
  （D15），仓库 `models/` 已移除；**安装器默认不带模型**，离线开箱包用
  `build_core.py --with-models`（统一根 → `resources/models/` → 安装目录）
- 不要在仓库目录构建 exe —— 产物继承 Low 标签，钩子失效（G24）
- 不要带着旧进程跑构建 —— `tauri build` 前先杀 `xifeng.exe`（它锁自己的产物，
  os error 5，G37）；NSIS 安装包装不上先复制出仓库剥 Low 标签（G38 真根因）
- 改 VAD 阈值 / 分词规则必须跑 golden 用例，否则会静默误伤
- 不要为了"便携"把 SQLite 放 exe 同级 —— 重建 dist 会清空（G25）
- 不要让小模型"自由改写" —— 守卫必须拒绝删词/大改/解释性输出，宁可漏改（G32）
- 不要在 Rust 正则里写 lookahead —— `regex` crate 不支持 lookaround，用"吞边界再吐回"
- 不要把 `xifeng_rs.pyd` 拷进仓库 —— 它属于 venv/site-packages，构建走 `tools/build_ext.py`
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
- Rust 工具链 / PyO3 扩展怎么编：`docs/02-guides/rust-env.md`

## 目录约定

```
Xifeng/
├── AGENTS.md          # 只放这一个文档
├── package.json       # pnpm workspace 根（脚本入口，别在这加依赖）
├── pnpm-workspace.yaml
├── docs/
│   ├── llms.txt
│   ├── 00-process/    # intent / spec / plan / review / sdlc（SDLC 工件）
│   ├── 01-overview/   # architecture / latency-budget
│   ├── 02-guides/     # quickstart / packaging
│   ├── 03-core-concepts/
│   ├── 05-gotchas.md
│   └── reference/     # 闲聊.md 等原始素材
├── apps/
│   └── desktop/       # Tauri 壳 + Web 主界面（pnpm 包 @xifeng/desktop）
│       ├── src/       # React 五页 + 浮窗页
│       └── src-tauri/ # Rust 壳：hotkey / voice / inject / py(IPC)
├── core/              # Python 核（uv 项目）：xifeng/ tests/ tools/ packaging/
│   └── run_spike.py   # 入口名是运行时契约，改名需同步 py.rs 与安装器
├── packages/          # 预留共享 TS 包（见 packages/README.md）
└── crates/            # Rust 核心（PyO3 扩展）：ime-post 规则 / ime-vad 算法 / ime-py 转接
```
