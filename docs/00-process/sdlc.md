# Voice IME 开发流程（本项目执行版）

> 这是**每天真正照着做**的流程文档，不是通用理论。
> 通用范式见：[SDLC 人机协同范式](../reference/ai-native-sdlc-playbook.md)（参考用，不必每次读）
>
> 适用对象：本地优先的 Windows 桌面语音输入法（Rust 核心 + PyO3 嵌入 Python 推理 + Tauri GUI），单人/两人小队开发。

---

## 0. 为什么不能照搬通用版

官方 playbook 面向**受监管的大型组织**：多角色交接、变更委员会、发布授权、合规审计。本项目是单人本地开发，直接照搬会产生大量空转。

裁剪原则：**保留"工件链 + 闸门"的骨架，砍掉"组织级治理"的皮。**

| 官方 play | 本项目处理 | 原因 |
|---|---|---|
| intent.md | ✅ 保留，但压缩到半页 | 现在最大的问题就是 `闲聊.md` 135KB 没有结论 |
| spec.md（Design） | ✅ 保留，且**必须含延迟预算 + 进程边界契约** | 返工最贵的地方在 Rust/Python 边界 |
| plan mode | ✅ 保留 | 单人项目最容易"想到哪写到哪"，plan 是唯一约束 |
| AGENTS.md（原 CLAUDE.md） | ✅ 保留 | 上下文资产是 agent 效果上限；本仓库统一用 `AGENTS.md` |
| skills | ✅ 保留 4 个（见 §4） | 只留会被反复违反的规则 |
| hooks | ⚠️ 精简到 3 个 | 没有审批委员会，只需要防"手滑" |
| 并行会话 / subagents | ✅ 保留，2 条起步 | 单人也能并行；verifier 兼作"第二双眼睛" |
| 反馈回路 | ✅ **升级为核心**，含 bench | 本项目的主要指标是延迟，不是功能对错 |
| 持续 evals | ✅ 保留，但要分两类（见 §6） | agent 配置 eval ≠ 产品指标 eval |
| AI PR 评审 | ⚠️ 降级为"独立上下文复审" | 没有同事，用新会话 + 隔夜复审代替 |
| hooks 作为审批闸门 | ❌ 砍掉 | 无发布经理、无变更单 |
| CI/CD 集成 | ✅ 轻量：本地一条命令跑全量 | 不引入流水线，但要一条命令 |
| 闭环监控 | ⚠️ 降级为本地看板 | 无生产环境，日志 + Trace 环形缓冲即可 |
| 定期代码库扫描 | ❌ 砍掉 | 单人小代码库，靠 clippy + review |
| Claude Tag 值班 | ❌ 砍掉 | 无 Slack 流程 |
| managed settings / 合规 API | ❌ 砍掉 | 无 IT 管控需求 |

---

## 1. 项目约束（所有判断的前提）

| 项 | 值 | 影响 |
|---|---|---|
| **延迟 SLO** | 端到端 **P95 ≤ 1.5s**（PTT 目标 ~620ms，VAD 模式 ~860ms） | 任何改动都要报延迟 |
| 平台 | Windows 10/11 x64 | 注入用 SendInput，热键用 rdev，需避让输入法切换 |
| 核心栈 | Rust（音频/VAD/状态机/注入）+ PyO3 + Python（ASR/LLM）+ Tauri 2（GUI） | 进程/语言边界是主要风险 |
| 模型 | SenseVoice-Small ONNX int8；Silero VAD v5；Qwen2.5-0.5B/1.5B GGUF | 模型文件不进仓库 |
| 团队 | 1–2 人 | 评审独立性靠机制补偿 |
| 阶段 | **本地开发跑起来**（未到分发） | 不做签名、更新通道、遥测上报 |

### 延迟预算（写进 spec，bench 以此为准）

| 阶段 | PTT | VAD |
|---|---|---|
| 尾点检测 | 0 | ~240ms |
| 切片/重采样 | <5ms | <5ms |
| ASR（3s 音频，CPU int8） | ~350ms | ~350ms |
| 文本清理 | <1ms | <1ms |
| 模型纠错（0.5B Q4，默认关） | 350–1500ms | 350–1500ms |
| 注入 | ~10ms | ~10ms |
| **合计** | **≈620ms（纠错关）** | **≈860ms（纠错关）** |

> 纠错默认关：0.5B 输出不稳定（会吞词、会续写），只能靠守卫保证"不改坏"，
> 不能保证"改对"，所以不作为默认路径（G32）。开启时 P95 可能破 1.5s，由用户显式选择。

---

## 2. 五道闸门（人只在这五个点判断）

```
G0 意图 ──→ G1 契约 ──→ G2 计划 ──→ G3 可运行证据 ──→ G4 合并
  ↑                                                      │
  └──────────── 指标回归失败 / 新需求 ────────────────────┘
```

| 门 | 判据 | 产物 | 谁批 |
|---|---|---|---|
| **G0 意图** | 能否用一句话说清"现在做不到什么、做完能做什么、怎么算成功" | `intent.md`（≤半页） | 你（作为产品负责人） |
| **G1 契约** | Rust↔Python 的输入输出、错误码、超时、内存所有权是否写死 | `spec.md` 的 §Interface 段 | 你（作为架构师） |
| **G2 计划** | 没参与对话的人能否照着 `plan.md` 实现 | `plan.md` | 你（作为工程师） |
| **G3 可运行证据** | 一条命令跑完测试 + bench + golden audio，且延迟不退化 | bench 输出贴在 PR/commit 说明 | 自动 + 人工确认 |
| **G4 合并** | diff 与 plan 一致；无未处理评审意见 | 合并提交 | 你（作为守门人） |

**关键**：G2 之后才允许写实现代码。这条是防止"边写边改需求"的唯一硬约束。

---

## 3. 工件与目录

```
Xifeng/                         # 仓库根只放 AGENTS.md + 代码，文档一律进 docs/
├── AGENTS.md                 # 常驻上下文（≤1 页）
├── docs/
│   ├── llms.txt              # 文档导航入口
│   ├── 00-process/           # SDLC 工件
│   │   ├── intent.md         # G0：要什么、为什么、成功标准
│   │   ├── spec.md           # G1：需求 + 延迟预算 + 进程边界契约
│   │   ├── plan.md           # G2：改哪些文件、顺序、风险、怎么证明
│   │   ├── review.md         # 复审清单（给自己/给评审 agent）
│   │   └── sdlc.md           # 本文件
│   ├── 01-overview/          # architecture / latency-budget
│   ├── 02-guides/            # quickstart / build / model-export
│   ├── 03-core-concepts/     # pipeline / vad / postprocess / correction / injection
│   ├── 04-reference/         # ipc-protocol / config / metrics
│   ├── 05-gotchas.md         # 坑位清单
│   └── reference/            # 原始素材（闲聊.md）
├── core/                   # Python 核（uv 项目）：xifeng/ tests/ tools/ packaging/
│   ├── xifeng/               # asr / correction / vad / postprocess / store / ipc
│   ├── tests/                # pytest 用例（330 个）
│   ├── tools/                # build_ext.py / core_smoke.py / dev-env.cmd
│   └── packaging/            # build_core.py（sidecar）/ build.py（PyInstaller）
├── crates/
│   ├── ime-post/             # 文本规则（与 core/xifeng/postprocess.py 等价）
│   ├── ime-vad/              # VAD 算法（与 core/xifeng/vad.py 等价）
│   └── ime-py/               # PyO3 边界：Python 签名 ↔ Rust
├── apps/
│   └── desktop/              # Tauri 壳 + Web 五页（pnpm 包 @xifeng/desktop）
│       ├── src/              # React 页面
│       └── src-tauri/        # Rust 壳：hotkey / voice / inject / py(IPC)
└── packages/                 # 预留共享 TS 包
```

> ⚠ 上面这棵树是 **M0 计划期的目标态**（当时打算 `gui/` + `crates/ime-core..ime-service`
> + `core/ime_infer/` + 仓库内 `models/`）。实现走了另一条路（D6 推迟 Rust、D16 触发链进壳、
> D15 模型移出仓库），**实际布局以根 `AGENTS.md`「目录约定」为准**：
> `apps/desktop/`（Tauri 壳 + Web UI）、`core/`（Python 核）、`crates/`（仅 ime-post / ime-vad / ime-py）。
> 模型在 `%LOCALAPPDATA%\Xifeng\models`，不在仓库。

### 工件规则

- `intent.md` / `spec.md` / `plan.md` 都进 git，改动即历史
- **plan 与实现不一致时，在同一个 commit 改 plan**
- 模型不进仓库（D15，统一根 `%LOCALAPPDATA%\Xifeng\models`）；下载清单记在
  `core/fetch_model.py` 的参数里，不落 `models/manifest.yaml`
- 任何超过 10MB 的文件不提交（hook 强制）

---

## 4. 上下文资产

### 4.1 AGENTS.md（≤1 页，只写 agent 会犯错的地方）

```markdown
# Voice IME

## 命令
- 编译: cargo build --workspace
- 测试: cargo test --workspace
- Python: pytest core/
- BENCH: cargo run -p benches --release （输出各阶段 P50/P95）
- 全量验证: ./verify.ps1 （test + bench + golden audio）

## 硬约束
- 音频路径禁止 unwrap/panic；错误必须映射成 ImeError
- 音频回调线程里禁止分配、禁止 IO、禁止加锁
- 模型常驻内存，禁止按次加载
- 端到端 P95 退化超过 50ms 视为回归，必须说明原因

## 架构
- ime-service(主进程) --PyO3--> core/ime_infer
- 状态机: Idle→Recording→Decoding→Correcting→Injecting
- 每次输入产生一条 Trace（环形存最近 100 条）

## 常见错误
- 不要在 Rust 侧拷贝 PCM 给 Python 前做 Vec<i16>→Vec<f32> 全量转换，走 zero-copy 视图
- VAD 阈值改动必须同时跑 golden audio，否则会静默切错句
- 不要升级 tauri / ort 大版本（等专门任务）
```

### 4.2 四个 skill（只留会被反复违反的）

| skill | 触发时机 | 内容要点 |
|---|---|---|
| `latency-budget` | 改音频/推理/注入路径 | 必须报告各阶段耗时增量；给出预算余量 |
| `rust-py-boundary` | 改 PyO3 接口 | 零拷贝、错误映射、GIL 持有时间、超时 |
| `windows-injection` | 改注入逻辑 | SendInput UNICODE、焦点切换、失败重试、不吞键 |
| `model-manifest` | 改模型加载/切换 | 校验 sha256、路径约定、量化标识、回滚旧模型 |

> 判断标准：**同一条规则被违反两次 → 写成 skill；违反会直接搞坏功能 → 写成 hook。**

### 4.3 三个 hook（精简版）

| hook | 动作 | 原因 |
|---|---|---|
| `block-large-files` | 阻止提交 >10MB 或 `models/**` | 防止仓库被模型文件撑爆 |
| `block-test-edits` | 修复任务中禁止改 `tests/golden/**` | 防止"改测试让它过" |
| `fmt-after-edit` | 编辑 .rs 后自动 `cargo fmt` | 单人没人 review 格式，交给工具 |

---

## 5. 反馈回路（本项目最重要的一节）

普通项目"测试通过 = 完成"；本项目**"延迟达标 + 识别正确 + 注入成功" 才算完成**。

### 四条回路，从快到慢

| # | 回路 | 命令 | 耗时 | 何时跑 |
|---|---|---|---|---|
| 1 | 单元/集成 | `cargo test` + `pytest` | 秒级 | 每次改完 |
| 2 | 延迟基准 | `cargo bench` / `benches/` | 十秒级 | 改音频/推理/注入路径必跑 |
| 3 | Golden audio 回归 | `./verify.ps1 audio` | 分钟级 | 改 VAD/ASR/纠错/模型必跑 |
| 4 | 注入 smoke | `./verify.ps1 inject` | 十秒级 | 改热键/注入必跑 |

### 回路设计要点

**① 延迟基准必须可复现**
- 固定测试音频（3s / 8s / 15s 各一），固定线程数，固定 warmup
- 输出结构化：`{stage, p50, p95, p99, budget, pass}`
- **首次运行先做 warmup**（1s 静音空跑），否则首次 500ms+ 冷启动会污染数据

**② 无音频硬件也能测**
- 状态机、切片、重采样、纠错全部用 PCM fixture 驱动
- 只有"真麦克风采集"依赖硬件，单独隔离成一个手测项

**③ Golden audio 是产品级 eval**
```
tests/golden/
  clean_short.wav   → expected: "打开设置面板。"      CER ≤ 5%
  noisy_street.wav  → expected: "帮我把这个文件发给他。" CER ≤ 10%
  long_pause.wav    → expected: 两句，切分正确
  english_mix.wav   → expected: 中英混排保持不变
```
判据：**CER 不退化 + 切分正确 + 端到端 P95 不退化**，三者同时满足才算过。

**④ 注入 smoke 必须落到真实目标**
- 起一个记事本窗口 → 触发注入 → 读回文本比对
- 验证点：焦点不丢、不吞键、中文不乱码、长文本不截断

### 完成定义（DoD）

一个变更算"完成"，当且仅当：

- [ ] `cargo test` / `pytest` 全绿
- [ ] bench 报告已附，各阶段在预算内（或说明为何超）
- [ ] 相关 golden audio CER 未退化
- [ ] 触摸音频/注入路径的改动，跑过注入 smoke
- [ ] `plan.md` 与实际 diff 一致
- [ ] `AGENTS.md` 里没有因此过期的规则

---

## 6. 两类 evals（不要混）

| 类型 | 目的 | 触发 | 判据 |
|---|---|---|---|
| **Agent eval** | 验证"给 agent 的配置"还能让它干对活 | 改 `AGENTS.md` / skills / hooks / 换模型 | 20 个真实任务的通过率不降 |
| **Product eval** | 验证产品指标没坏 | 改 ASR / VAD / 纠错 / 量化 / 模型 | CER、切分准确率、P95 延迟 |

**Agent eval 的任务池**（20 条起，从真实工作中攒）：
- "给 Injector 加一个失败重试，最多 2 次"
- "把 VAD 静音判定从 240ms 改成可配置"
- "给 Trace 加一个 p99 统计"
- …每条判据都是：编译过 + 测试过 + bench 不退化

**Product eval 的门槛**：
- 中文干净语音 CER ≤ 5%
- 噪声场景 CER ≤ 10%
- 中英混排不出现整体转英文
- 端到端 P95 ≤ 1500ms（PTT 模式 ≤ 800ms）
- 注入成功率 ≥ 99%（100 次注入 0 失败）

> 每次换模型、改量化、调 VAD 阈值 → 必须跑 Product eval，不许凭感觉。

---

## 7. 里程碑（先跑起来，再优化）

| 里程碑 | 目标 | 完成判据 | 明确不做 |
|---|---|---|---|
| **M0 Walking skeleton** | 按住热键 → 录音 → ASR → 注入记事本，全链路打通 | 能连续 10 次正确输入；有 Trace 输出；P95 有数（不设目标） | 不做 VAD、不做纠错、不做 GUI、不做模型切换 |
| **M1 延迟达标** | 加 VAD + warmup + 规则纠错 | PTT P95 ≤ 800ms；VAD 模式 ≤ 1000ms；golden audio 4 条全过 | 不接 LLM 纠错（规则层后于 v0.23 下线） |
| **M2 纠错** | 接 Qwen 0.5B + 守卫 | 守卫能拦住删词/大改/解释性输出；默认关；开启时延时可量 | 不做自定义热词、不做自动降级 |
| **M3 模型管理与 GUI** | 下载/校验/切换 + 统计面板 | manifest 校验生效；切换后不掉延迟；面板能看到 P50/P95/CER | 不做账号、不做云同步 |
| **M4 本地可用** | 打包 + 开机自启 + 托盘 | 安装后在新用户目录可用；崩溃可恢复；日志可定位 | 不做签名、不做自动更新 |

**M0 是硬性要求**：没有 M0 就不许优化延迟。先有链路，再有数字，再有优化。

---

## 8. 度量看板（本地，一个页面）

| 层 | 指标 | 目标 | 来源 |
|---|---|---|---|
| **北极星** | 端到端 P95 | ≤1500ms | Trace 环形缓冲 |
| 北极星 | 干净语音 CER | ≤5% | golden audio |
| 领先 | 一次实现即通过率 | ≥60% | 每任务记录 |
| 领先 | 延迟预算余量 | ≥30% | bench |
| 滞后 | 注入失败率 | ≤1% | 注入 smoke |
| 滞后 | 回归逃逸数（M 阶段后发现） | 0 | 里程碑复盘 |
| 成本 | 单次输入 token/算力成本 | 0（全本地） | — |

**红线**：功能变多但 P95 上升 → 先停下来优化，不许继续加功能。

---

## 9. 单人开发如何保留"评审独立性"

没有同事，最容易出的问题是**自己给自己盖章**。三个补偿机制：

1. **独立上下文复审**：写完后**开新会话**（不是继续原对话），只给 diff + `review.md` + `plan.md`，让它挑错。原会话里的假设不会污染判断。
2. **隔夜复审**：当天写完不合并，第二天用 10 分钟只看 diff 的"意图和影响面"。睡眠是免费的上下文隔离。
3. **verifier subagent**：只报告不修改，且**不能看实现过程**，只看行为是否符合 `plan.md`。

`review.md`（本项目版）：

```markdown
# 复审清单

## 必查
- 音频回调线程里是否引入了分配/锁/IO
- 错误路径是否可能 panic（unwrap/expect/index）
- PyO3 边界是否拷贝了整段 PCM
- 新增路径是否有延迟数字

## 判为 Important
- 会让 P95 退化 >50ms
- 会让某种音频输入直接崩溃或静默失败
- 会让注入丢字/乱码

## 不算问题
- 命名风格、注释多少
- 测试覆盖率数字本身
```

---

## 10. 本项目特有的反模式

| 反模式 | 后果 | 对策 |
|---|---|---|
| 先做 GUI 再做链路 | 永远跑不起来 | M0 必须无 GUI |
| 没有 benchmark 就调延迟 | 靠感觉优化，越调越慢 | bench 先于优化 |
| 模型文件提交进 git | 仓库废掉 | hook + manifest |
| 用真实麦克风做自动化测试 | 不可复现、CI 跑不了 | PCM fixture 驱动 |
| 改 VAD 阈值不跑 golden audio | 静默切错句，很难发现 | 强制 product eval |
| 纠错失败就当识别失败 | 掩盖真实 CER | 分层记录 ASR 与纠错结果 |
| 一次性把 5 个模块一起写 | 出问题无法定位 | 一次一个 crate，先跑通再优化 |
| 让 agent 顺手"优化"音频线程 | 延迟抖动、偶发爆音 | 音频路径列为受保护，改动需显式说明 |

---

## 11. 现在就开始的三件事

1. **把 `Xifeng/docs/reference/闲聊.md` 蒸馏成 `intent.md`**（半页）——135KB 聊天记录不是工件，是原料。
2. **写 `spec.md` 的 §Interface 段**：Rust→Python 的 `transcribe(pcm: &[i16], sample_rate: u32) -> Result<Transcript, ImeError>`，含超时、错误码、内存所有权。
3. **立 M0 骨架**：`ime-audio` 能录 → `ime-infer` 能转 → `ime-inject` 能打。先不要 VAD，不要纠错，不要 GUI。

> 判据只有一句：**能不能连续 10 次把说的话打进记事本。**
