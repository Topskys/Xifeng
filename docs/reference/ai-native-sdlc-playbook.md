# The AI-native SDLC Playbook（中文精读版）

> 原文：Anthropic《The AI-native SDLC playbook》
> 作者：Louis Claxton ｜ 发布：2026-08-21 ｜ 阅读时长：约 40 分钟
>
> - 完整长文：<https://claude.com/blog/the-ai-native-sdlc-playbook>
> - 官方课程（14 节 / 约 1 小时）：<https://academy.claude.com/courses/ai-native-sdlc-playbook>
> - 配套：Anthropic 如何保障自己的 AI-native SDLC
>   <https://claude.com/resources/articles/how-anthropic-secures-its-ai-native-software-development-lifecycle>
>
> 本文是内容精读与结构化整理，非逐字翻译。面向**已在使用 Claude Code、但审批/评审/交接仍按人类速度运行**的工程、平台和安全负责人，尤其适合受监管的大型组织。

---

## 目录

1. [核心论点](#1-核心论点)
2. [六阶段对照](#2-六阶段对照)
3. [最关键的设计：工件链即审计链](#3-最关键的设计工件链即审计链)
4. [Play 详解](#4-play-详解)
5. [人与 agent 如何协同](#5-人与-agent-如何协同)
6. [治理与安全控制](#6-治理与安全控制)
7. [度量方式](#7-度量方式)
8. [落地顺序与反模式](#8-落地顺序与反模式)
9. [模板附录](#9-模板附录)

---

## 1. 核心论点

**代码不再是瓶颈，build 两侧的人工速度环节才是**——主要是 Plan、Review/Test、Deploy。

传统 SDLC 的流程重量（PRD、估算仪式、安全评审）是为了在"写代码最贵"的年代强制对齐，它默认每一步都由人执行。当 agent 把 build 压缩到小时级，三个后果同时出现：

| 后果 | 具体表现 |
|---|---|
| 瓶颈转移 | 计划、评审/测试、部署仍以人类速度运行，长度不变 |
| 控制点错位 | 逐行评审在人写代码时成立；agent 写大部分 diff 后不可持续 |
| 治理成本上升 | 例外仍走周会/月会审批，流程空转 |

以安全团队为例：安全团队按人类产出定编，agent 放大代码产量后只有两种结局——**队列堆积**或**审查不足**。受监管组织两者都不能接受，所以安全与合规检查必须跟上 agent 的速度。

> 结论：传统 SDLC 需要和实现阶段同等量级的改造，而不只是"在写代码环节用 AI"。

### 什么是 AI-native SDLC

不是线性流程，而是**闭环**：AI 嵌入每个节点，每个被接受的工件自动触发下一道闸门，取代传统阶段间笨重的手工交接。

（同义说法：agentic SDLC、AI SDLC、agentic software development。）

---

## 2. 六阶段对照

| 阶段 | 传统 SDLC | AI-native SDLC |
|---|---|---|
| **Plan** | 委员会收集需求，工作坊 + 签字，手工成文 | Claude 从原始来源直接综合痛点，写入 `intent.md`（人类可读 + 机器可执行）|
| **Design** | 分析师写规格，设计师再解析 | 需求与设计压缩为一次工作会话，策略以 skill 编码并纳入 git |
| **Build** | 手写代码与测试，事后补文档 | AI 生成代码与测试；机构知识变成版本化的 `CLAUDE.md` 与 skills |
| **Test** | 阶段边界的 QA 闸门 | 贯穿实现过程的持续 evals |
| **Deploy** | 人工逐行评审，治理散落在评审周期中且不一致 | 多层 agent 评审，人只审受监管/关键代码；治理在 agent 行动时由 hook 强制执行 |
| **Maintain** | 人盯生产找 bug | agent 监控线上；控制带被击穿即诊断，并写回为新的 `intent.md` |

**贯穿主线**：右列每一阶段都以"向版本控制提交一个工件"结束，下一阶段以读取它开始。人仍然对每个需要判断力的决策负责，只是注意力随工件移动。

---

## 3. 最关键的设计：工件链即审计链

```
intent.md ──[产品负责人批准]──→ spec.md ──[产品负责人批准]──→ plan.md ──[工程师接受]──→
代码 + 测试 ──[PR 评审闸门]──→ 合并 ──→ 部署 ──[release 授权]──→ 生产
                                                                    │
                                          控制带击穿 ──→ 新的 intent.md（闭环重启）
```

- 早期阶段以 `.md` 为主，因为**产品负责人和 agent 能读同一份文件**
- Build 之后，工件是代码及其记录
- **commit 链就是审计轨迹**：谁提的、agent 产出了什么、谁批的
- 触发关系：接受 `intent.md` → 触发设计；批准 `spec.md` → 触发 plan mode；合并 PR → 触发流水线；控制带击穿 → 写下一条 intent

### 源唯一性（transition 期必做）

现有体系里工件可能散落各处（Jira、需求管理工具、Figma、变更委员会）。**为每个工件指定唯一事实源**，其余只存副本或链接。三种可选配置：

| 配置 | 做法 | 适用 |
|---|---|---|
| **仓库为事实源** | markdown 工件即权威记录，遗留系统引用 commit | 工程主导型组织，最干净 |
| **遗留系统为事实源** | Jira/ServiceNow 持有权威记录，markdown 是工作副本；会话开始读记录，结束经 MCP 写回 | 已有强监管追溯要求 |
| **双向链接（最低标准）** | 工件记录 ID，遗留记录存 commit SHA | 过渡期，接受双事实源 |

两种体系可以共存，前提是**有链接，或明确声明一个是事实源**。

---

## 4. Play 详解

每个 play 固定包含：变化点（What changes）、起步（Getting started，含前置条件）、执行步骤、治理考量、如何度量。

Play 按六个阶段分组，但**采纳顺序不等于阶段顺序**——有依赖图：无入边的 play 可以随时先做。

### 阶段一：Plan

#### Play 1｜Capture as intent.md

**变化点**
- 传统：想法要经过 backlog、用户故事、故事点、refinement 会议；每次交接都转移 ownership，到工程手里时已偏离原意
- AI-native：提出者与 Claude 头脑风暴，用自己的语言写成 `intent.md`（proto-spec）

**执行**
1. 提出者用自己的话描述问题（不能做什么、谁受影响、什么算更好、什么不在范围）
2. 与 Claude 头脑风暴到具体（scope / users / constraints / success）
3. 让 Claude 按组织模板写成 `intent.md`（模板可做成 skill）
4. 提出者纠正误解
5. 提交到共享的 intent 目录（单人产品用仓库内 `intent/`；跨仓库才值得独立 repo）

**关键设计**：非工程人员不需要会用 git —— 通过 GitHub 等连接器让 Claude 代为提交。

**治理**：证据就是已提交的 `intent.md`（作者、时间戳、完整修订史）。产品负责人的接受/拒绝记录为 merge 或关闭的 review。

**度量**
- 领先：首次对话 → 提交 `intent.md` 的时间（预期从数周降到小时）
- 滞后：intent 存活率（被接受进入 Design 的比例）；首个 `spec.md` commit 之后仍对 intent 的修改次数

### 阶段二：Design

#### Play 2｜Requirements and design

**变化点**：需求与设计从两个团队两个阶段，压缩成一次带策略约束的会话。

**执行**
1. 产品负责人挂载 `intent.md`，加载组织 skills（品牌/安全/合规/UX）
2. 提示词指向 intent、点名约束、**要求标注关注点**（尤其无法同时满足的冲突策略）
3. 人工评审 spec 是否解决原问题、open questions 是否被回答或结转
4. 先处理被标记的关注点——那正是分析师会升级的问题，由产品负责人与对应政策 owner 解决
5. 提交 `spec.md`（与 `intent.md` 成对，记录"要什么"和"定了什么"）
6. 人工决定是否进入 build（高风险咨询 tech lead）

**成熟路径**：手工跑 → 固化为组织级 slash command → 最终以 intent merge 为触发器跑非交互任务并提 PR。

**治理**：策略在写 spec 时就被应用，而不是几周后在评审里被发现。spec、产生它的 prompt、生效的 skill 版本全部入版本控制。

**度量**
- 领先：`intent.md` commit → `spec.md` commit 的间隔
- 滞后：build 开始后的需求返工（首个 `plan.md` 之后的 `spec.md` commit 数）

### 阶段三：Build

#### Play 3｜plan mode 作为默认起点

**变化点**：先有可评审、可提交的 `plan.md`，而不是让评审者第一次看到成品 diff。

**执行**
1. 会话从 plan mode 开始（只读，不能改文件）
2. 给 Claude `intent.md` + `spec.md`，要求计划写明：改哪些文件、工作顺序、用什么测试证明
3. **盘问计划**：这变更可能破坏什么？哪一步最危险？放弃了哪些方案？
4. 迭代到"没参与对话的人也能照着实现"
5. 提交 `plan.md`，后续 PR 评审以它为基准
6. 接受后让 Claude 实现（计划够好时常常一遍过）
7. 实现偏离计划时，**在同一 commit 更新 `plan.md`**（可用 hook 强制同步）

**治理**：设计评审发生在生成任何代码之前，改方向只需改文档。plan mode 本身即强制——**工程师接受计划前 Claude 无法编辑文件**。

**度量**
- 领先：一次实现即合并的比例；计划批准 → PR 合并的时长
- 滞后：每变更返工轮次；合并 diff 与 `plan.md` 的一致率

#### Play 4｜CLAUDE.md

给 Claude 一个"新入职者需要知道的全部"：约定、命令、架构、团队最常犯的错。

**执行**
1. `/init` 生成初版
2. **砍到一页以内**——新人第一天真正需要的、最重要的约定、Claude 反复犯的错
3. 提交到仓库根目录，团队共享同一版本，变更走代码评审
4. 工作规则：**同一个错误犯两次，就把纠正写进 CLAUDE.md**
5. 保持精简，过期内容只是在白占上下文

```markdown
# Payments service

## Commands
- Build: make build
- Test: make test (unit), make itest (integration, needs docker)
- Lint: make lint (runs in CI; fix before pushing)

## Conventions
- Java 21, Spring Boot 3. No new Lombok.
- Money is always BigDecimal, never double.
- Every endpoint needs an integration test in src/itest.

## Architecture
- api/ holds REST controllers, core/ holds domain logic,
  adapters/ talks to external systems.
- Kafka events are defined in schemas/; never edit generated classes.

## Things Claude gets wrong
- Do not bump dependency versions; the platform team owns them.
- The legacy v1/ package is frozen; changes go in v2/.
```

**度量**
- 领先：Claude 重复 CLAUDE.md 本应拦住的错误的次数
- 滞后：新成员首个 PR 合并的时间

#### Play 5｜Skills 作为机构知识

**判断标准**：需要**一致应用**的机构知识写成 skill；属于 `CLAUDE.md` 或一次性 prompt 的内容不要写成 skill。

**执行**
1. 挑一条目前执行不一致的知识（安全标准、API 约定、品牌规则）
2. 写成 `.claude/skills/<name>/SKILL.md`：frontmatter 声明何时触发，正文写做什么
3. 放仓库内随代码走，或经 plugin marketplace 组织级分发
4. **测试触发**：用不同问法让 Claude 做相关任务，确认每次都加载
5. 政策变更时改 skill，由政策 owner 签核

```markdown
---
name: secure-api-review
description: Apply the API security standard. Use whenever creating or
  modifying an external-facing endpoint, reviewing API code, or
  generating an OpenAPI spec.
---
# Secure API review

When you create or change an API endpoint:
1. Authentication: every endpoint requires the gateway JWT;
   no anonymous routes outside /health.
2. Input validation: validate request bodies against the OpenAPI
   schema and reject unknown fields.
3. Audit: every state-changing endpoint emits an audit event with
   actor, action, entity and timestamp.
4. Data classification: fields tagged pii in the schema must never
   appear in logs or error messages.

Run scripts/check-endpoints.sh and include its output in your summary.
```

**治理（核心判断）**：**skill 是建议性控制，不是强制控制**——它让 Claude 更可能遵守，但没有任何东西强制会话服从。必须永远成立的政策，需要确定性手段兜底：hook 阻止动作，或 PR 评审复查。**skill 让违规变少，hook 让违规几乎不可能。**

**度量**
- 领先：政策批准 → skill 变更合并的时间
- 滞后：引用该政策的评审发现数（应趋近 0；不降说明 skill 没触发或文本已漂移）

#### Play 6｜Hooks 作为构建期护栏

Build 期 Claude 的动作主要是文件编辑和 shell 命令，也是 hook 触发最频繁的地方。

可做的事：
- 阻止编辑受保护路径（生成代码、冻结包）
- 编辑后自动格式化/检查，防止漂移累积
- 阻止凭据进入 diff

**原则**：需要 100% 成立的政策，背后必须有 hook。build 期 hook 要**快且只针对变更文件**；全量测试放 commit 或 PR 阶段。**要求人工批准的 hook 属于 Deploy 阶段的闸门**——build 期弹审批会把一个人重新放回所有并行会话的关键路径上。

#### Play 7｜并行会话与 subagents

| 概念 | 定义 | 作用 |
|---|---|---|
| 并行会话 | 另一个完整 Claude Code 实例，在自己的 git worktree 里做另一个任务 | 提高单人并行任务数；**会话之间互不知情，唯一的共享状态是工程师** |
| subagent | 单会话内的受限助手，独立上下文窗口与工具权限 | 让主会话保持聚焦；适合重复性工作 |

**执行**
1. 按 `plan.md` 判断哪些任务改的文件不重叠；共享文件的任务串行
2. 每个并行任务一个 worktree（`claude --worktree feature-auth`）
3. **2–3 个起步**；上限取决于一个人能认真评审多少条流
4. 把重复岗位固化成 `.claude/agents/*.md`：verifier（跑起来验证行为）、simplifier（删多余复杂度）、researcher（探索代码库但不污染主上下文）

```markdown
---
name: verifier
description: Runs the app and checks the change works before the session
  reports done
tools: Bash, Read
---
Start the app with make run. Exercise the changed behavior and the two
nearest neighboring flows. Report what you ran, what you saw, and any
behavior that does not match plan.md. Do not fix anything; report only.
```

**治理**：会话越多产出越多，控制必须来自仓库里的配置（hooks、permission settings 对所有会话生效），且每个动作都可归因到发起它的工程师。

**度量**
- 领先：评审质量不降前提下的单人并行会话数；"编排"而非"等待"的时间占比
- 滞后：单人每周合并变更数（与返工率一起看）

### 阶段四：Test

#### Play 8｜给 Claude 反馈回路

**核心**：永远给 Claude 一个自己验证的办法（测试、构建、截图比对）。会话在人看到之前先自查自修。

> 注意区分：**反馈回路**贯穿整个任务、可反复多次；**verifier subagent** 是任务声称完成时用全新上下文跑一次的终检——这样结论不被"写出代码的那些假设"污染。

**执行**
1. 把多步检查包成单条命令（`make test`），失败非零退出
2. 在 `CLAUDE.md` 的 Commands 段列出每条命令 + 健康输出示例
3. 给出**可量化**的完成目标（"test_status.py 全绿"、"截图与 mock 一致"、"端点返回 200 且带新字段"）
4. **修 bug 先写失败测试**：让 Claude 复现为测试、确认因预期原因失败、提交该测试；然后才让它改到通过，**且不允许改测试文件**（用 hook 强制）——修复前就存在、且 agent 改不动的测试，才是 bug 已消失的证明
5. UI 工作必须闭环视觉：给浏览器/截图工具 + mock，实现→截图→比对→调整，2–3 轮正常
6. 把验证写进"完成"的定义
7. **保护回路本身**：agent 不能削弱检查它的代码

```markdown
## Verifying your work

- Build: make build (must finish with "Build succeeded")
- Test: make test (all green; never skip or delete a failing test)
- Lint: make lint (zero warnings)

Run all three before reporting any task complete, and paste the output.
If a test fails, fix the code, not the test.
```

**治理**：证据是 `make test` 的字面输出、构建日志、截图 diff —— 来自工具链而非 agent 自述。记录在会话 transcript（经 OpenTelemetry 导出）与 PR check run 中。

**度量**
- 领先：agent 变更的首次 CI 通过率
- 滞后：每 PR 评审时长；变更失败率

#### Play 9｜CI 中的持续 evals

**定位**：evals 是 AI-native 版的阶段门 QA —— 一套在 **agent 配置发生变化时**运行的测试。

模型换代、prompt 重写、skill 改动，都可能让 agent 达不到原来的标准。evals 回答"它还做得到吗"。

**执行**
1. 从近期真实工作中收集 20–50 个任务及其被接受的结果
2. 每个任务写成一个 eval：prompt + 判定通过的标准（测试过、lint 干净、行为不变、政策遵守）
3. 在 CI 中非交互运行：定时 + **任何 `CLAUDE.md` / skills / hooks 变更时**
4. **用结果门禁配置变更**——通过率下降的 skill 改动物不能直接合
5. **每个生产事故都变成一个 eval**，永久留在套件里当回归测试

```yaml
name: Agent evals
on:
  pull_request:
    paths: ['CLAUDE.md', '.claude/**']
  schedule:
    - cron: '0 2 * * *'
jobs:
  evals:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm install -g @anthropic-ai/claude-code
      - name: Run eval suite
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          for eval in evals/*.json; do
            claude -p "$(jq -r '.prompt' $eval)" \
              --allowedTools "Read,Edit,Bash(make test)" \
              --output-format json > result.json
            ./evals/check.sh "$eval" result.json
          done
```

**度量**
- 领先：eval 通过率趋势；事故 → 变成常驻 eval 的时长
- 滞后：CI 捕获的回归 vs 生产发现的回归

### 阶段五：Deploy

#### Play 10｜PR 评审回路中的 AI

Claude 既给评审也收评审：按组织政策审入站 PR；在自己的 PR 上响应评审意见。

**执行**
1. 起步最快是托管 Code Review 服务（管理员开启并选仓库）；需要自控流水线或用自有云协议时，用 `claude-code-action` 在自己的 CI 里跑
2. tech lead 写 `REVIEW.md`，按通道划分：bugs / security / compliance（对照 `spec.md`、`plan.md` 与设计原则）
3. **设定人工阈值**：发现本身不批准也不阻塞 PR，分支保护仍要求 code owner 批准；需要机器门禁时可读 check run 发布的严重度计数
4. 在评审评论里 `@claude`，它处理并推送修复；PR 线程同时记录请求与变更
5. **评审发现反哺 `CLAUDE.md`**：同一错误被标记第二次，纠正就写进去；评审也负责指出 CLAUDE.md 已过期
6. 每月调优：给发现打分以改进评审器，限制 Nit 数量，排除生成目录与 CI 已覆盖的内容

```markdown
# Review instructions

## Passes
Run three passes and tag each finding with its pass:
- Bugs: logic errors, broken edge cases, subtle regressions
- Security: injection risks, authentication gaps, PII in logs
- Compliance: the change matches spec.md, plan.md and our design principles

## What Important means here
Reserve Important for findings that would break behavior, leak data
or breach a policy. Style and naming are nits.

## Cap the nits
Report at most five nits per review; summarize the rest as a count.

## Do not report
Generated files under src/gen/ and anything CI already enforces.
```

**治理**：**分离职责由机制保证——写代码的 agent 无法批准代码**。评审政策对所有 PR 一致应用，findings/修复/评分/批准全部记录在 PR 历史里，PR 即审计记录。

**度量**
- 领先：首次评审时间（应降到分钟级）；无需人工介入即解决的评审评论占比
- 滞后：合并前捕获的缺陷/漏洞 vs 逃逸到生产的数量

#### Play 11｜Hooks 作为审批闸门

Build 期的 hook 是"允许/阻止"，Deploy 期的 hook 可以"询问"——暂停动作直到指定人批准。

**执行**
1. 工程领导层联合变更管理与合规，列出必须保留的人工审批门（变更管理签字、发布授权、受保护路径）
2. 每个门写成一个 hook（allow / ask / block）
3. 团队 hook 放 `.claude/settings.json` 入 git；**不可协商的放 managed settings**，工程师无法关闭
4. **block 必须能自我解释**：说明原因和获批路径

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/production-gate.sh" }
        ]
      }
    ]
  }
}
```

```bash
#!/bin/bash
# Production deploys require a named release authorization
cmd=$(jq -r '.tool_input.command' < /dev/stdin)
if [[ "$cmd" == *"deploy"* && "$cmd" == *"production"* ]]; then
   if [ -z "$RELEASE_APPROVAL" ]; then
     echo "Production deploys need a release authorization." >&2
     exit 2 # exit 2 blocks the action; the message goes to Claude
   fi
fi
exit 0
```

**治理**：闸门条件对所有人、每次执行。允许/阻止决策带时间戳记录。**闸门同时定义了"什么算批准"**——已批准的变更单，或发布经理的签字。

**度量**
- 领先：每个闸门的等待时长（OTel 导出含时间戳与判定）
- 滞后：hook 上线前后逃到生产的闸门违规数

#### Play 12｜CI/CD 集成与部署

**变化点**：流水线原来只跑确定性脚本，任何需要判断的环节（flaky 测试分诊、写变更日志、查构建失败原因）都要等人。

**执行**
1. **从只读判断步骤开始**：`claude -p` 分诊失败构建、总结 flaky 测试、起草 changelog
2. 写操作放在既有闸门之后（修 lint、更新生成文档、响应 `@claude`）；**agent 写的任何东西都以 PR 形式到达，没有直推 main 的路径**
3. 执行必须沙箱化：容器 + 网络策略 + 短期作用域 token，默认不持有生产凭据
4. **部署通过 MCP 暴露**：deploy / status / rollback 成为按环境限定的工具，而不是带凭据的 shell 脚本
5. **按环境分级自主度**：dev 自由部署；staging 居中；production 由 agent 准备、发布经理授权、hook 强制
6. **回滚必须是流水线里最常演练的路径**——单条命令、agent 可执行、定期在 staging 演练（Maintain 阶段的闭环会调用它）

```yaml
- name: Triage failed build
  if: failure()
  run: >
    claude -p "Read the build log at out/build.log. Identify the most
    likely cause, say whether the failure looks flaky or real, and write a
    three-line summary for the PR thread." >> triage.md
```

**治理原则**：**agent 可以一路做到生产闸门前，但不能越过它。**

- 分支保护把 agent 的产出变成 PR，没有直推 main 的路径
- 生产部署 hook 阻止发布直到指定发布经理授权
- 每次非交互运行以 agent 自己的身份执行，日志能区分"agent 做了什么"与"触发它的工程师做了什么"
- 按环境的权限分层决定 agent 到闸门前能做多少

**度量**
- 领先：无需呼人即完成分诊的流水线失败占比
- 滞后：DORA 指标（CI 与部署工具已能产出）

### 阶段六：Maintain

#### Play 13｜闭环：从指标回到 intent

前面所有阶段都需要人发起第一步；这个阶段是**无人启动的自主运行**。

**执行**
1. 选一个基线稳定的指标（CI 测试失败率、部署后 5xx 率、PR 周期）
2. 写检测脚本（滚动窗口均值/标准差 + Western Electric 类规则，能抓慢漂移也能抓尖峰）。**检测完全确定性，不引入模型**，脚本版本控制并单测
3. 响应分级写在版本控制配置里：

```yaml
metric: ci_test_failure_rate
baseline: rolling_30d
rules: western_electric
tiers:
  1sigma: { action: log }
  2sigma: { action: diagnose,
            tools: "Read,Grep,Bash(gh run view *)" }
  3sigma: { action: propose,
            routes: [pull_request, runbook:rollback-deploy] }
```

4. 触发器可以是定时 workflow、监控 webhook、或内网 Cron；Claude 无状态运行（CI 非交互步骤或 Agent SDK 服务），因此循环可以无人启动地开始和结束
5. **诊断写成 `intent.md`**（异常与证据、期望结果、受影响系统、open questions），之后走正常流程
6. 服务 owner / on-call 分诊：现在修 / 排期 / 驳回。**驳回用于调阈值、降噪**
7. 修复上线后，为该事故补一个 eval

**治理**：分级边界由版本控制配置强制，权限与管理设置禁止生产访问；调用、发现、分诊决策均带时间戳记录；变更仍走正常 PR 评审闸门；可触发的 runbook 均为预先批准。

**度量**
- 领先：控制带击穿 → 分诊队列里出现 `intent.md` 的时长
- 滞后：变成已合并修复的发现占比；同类事故复发率

**典型例子**
- CI 失败率破 3σ → 隔离 flaky 测试或开 revert PR，由评审闸门决定
- 部署窗口内 5xx 破 3σ → 触发既有回滚流水线
- PR 周期触发漂移规则 → 给工程领导层写报告（说明 harness 对流程指标同样有效）

#### Play 14｜定期代码库扫描

**判断**：一次扫描只是"某模型下某时刻"的断言，**代码和模型两半都会过期**——代码每周在变，每一代模型都能发现上一代漏掉的漏洞。

**执行**
1. 按 repo / 服务 / 团队组织成项目，明确发现归属
2. 对最关键仓库跑首次全量扫描（**首次扫描通常会命中"被认为干净"的代码**），以此为基线
3. 设定计划：活跃服务默认每周；大仓库/混合仓库限定目录或分支
4. 带着置信度分诊；**驳回必须给理由**，否则下次同样的问题会以"新发现"重现
5. 单个 PR 能修的：在 Claude Code on the Web 打开建议补丁、评审、走 PR 闸门。**提出修复的 agent 无权批准它**
6. 超出单 PR 范围的（架构弱点、跨服务重复模式）：写成 `intent.md`，从 Plan 开始
7. 修复上线后，把该漏洞类别加进 eval 套件
8. 导出 CSV/Markdown 或 webhook，让既有 tracker 与审计系统继续当记录系统

**治理**：仓库接入、扫描席位、消费上限均由管理端集中控制。每条发现有验证结果与置信度，每次驳回有理由 → 扫描历史即"发现了什么、修了什么、有意接受了什么"的审计记录。定期扫描**补充**既有 SAST/依赖扫描，不替代。

**度量**
- 领先：接入计划的仓库占比；发现 → 补丁进入评审闸门的时长
- 滞后：定期扫描发现 vs 生产/外部报告发现；多轮扫描后每轮发现数的下降趋势

#### Play 15｜Claude Tag 值班

事故常常以 Slack/Teams 消息的形式到达（晚上 10 点的一条急修请求）。Claude Tag（公测，Slack）以**独立身份**成为频道成员，每条新事故都有 first responder，响应过程本身进入循环与记忆。

- 对话与机构知识留在频道，频道内任何人都能引导和行动
- 可实时验证假设、探索方案，频道历史增强可审计性
- 经 MCP 验证指标回到基线并在 thread 确认，把复盘写进版本控制的 lessons 文件
- 不只是事故：在 ticket 上被 @ 时同样分诊——小而明确的修复以 PR 到达评审闸门，更大的写成 `intent.md` 从 Plan 开始

> **频道即审计轨迹**：请求、诊断、人工授权、修复都留在事故处理发生的地方。

---

## 5. 人与 agent 如何协同

### 一句话

**人与 agent 不共享上下文，共享工件；人不在流程里，人在闸门上。**

> "Human attention concentrates at the gates, reviewing what the agent flagged rather than starting each stage from scratch."

### 协同的三个机制

**① 工件交接**
每个阶段以一个 commit 结束，下一阶段读它开始。交接不需要开会、不需要转述。这使协作天然异步，且每一步都有记录。

**② 闸门收敛**
人在流程中只有几个明确的介入点，每个介入点都是一个**判断**而非**操作**。

**③ 指令分层**
人的意图沉淀成四种不同强度的载体：

| 载体 | 性质 | 谁维护 | 生效方式 |
|---|---|---|---|
| `prompt` | 一次性 | 当次操作者 | 当下 |
| `CLAUDE.md` | 常驻知识 | 全队，错误犯两次就写进去 | 每个会话自动读 |
| `skill` | 政策（**建议性**） | 政策 owner 签核 | 相关任务触发 |
| `hook` | 强制（**确定性**） | 平台/安全团队 | 每次匹配动作执行 |

可靠性差别就在这里：**靠 prompt 约定 = 靠人记得；靠 hook = 靠系统保证。**

### 谁在哪个点做什么

| 阶段 | Agent 做 | 人做 | 人的动作 |
|---|---|---|---|
| Plan | 综合痛点、按模板起草 `intent.md` | 提出者（可以完全不是工程师）纠正误解 | 改 + commit |
| Plan 末 | — | 产品负责人判断"要不要做" | 批准 / 关闭 |
| Design | 带 skills 展开成 `spec.md`，主动标注策略冲突 | 产品负责人**审但不写**；冲突路由给政策 owner | 审 + 路由 |
| Design 末 | — | 判断是否进 build（高风险咨询 tech lead） | 批准 spec |
| Build 起 | plan mode 只读探索，产出 `plan.md` | 工程师**盘问计划**：会破坏什么？哪步最险？放弃哪些方案？ | 迭代到"外人可照做" |
| Build | 实现、自测、修 lint | auto 模式下**不再看每次编辑**，改为看更长自主会话后的工件 | 接受计划 / 中断 |
| Test | 跑 build/test/lint、截图比对、修到通过 | 工程师定义"什么算完成"与反馈回路 | 配置一次，之后自动 |
| Review | 按 `REVIEW.md` 跑 bugs / security / compliance，按严重度排序 | code owner 判断**意图与风险** | 批准（分支保护强制） |
| Deploy | 准备发布、跑 pipeline 判断步骤、可触发回滚 | release manager 授权生产发布 | 授权（hook 强制） |
| Maintain | 监控、诊断、开 PR 或写新 intent | on-call / owner 分诊 | 现在修 / 排期 / 驳回 |

### 人的角色实际变成三个

| 角色 | 做什么 | 不可替代的原因 |
|---|---|---|
| **定义者** | 说清想要什么、为什么、约束是什么 | agent 无法替你决定"值不值得做" |
| **编排者** | 拆任务、开并行、选自主度、决定何时回滚重来 | 需要判断 blast radius |
| **守门人** | plan 批准、spec 批准、PR 批准、发布授权、事故结论 | 问责必须落在人身上 |

> 原文收尾："The loop keeps running. Human judgement stays above it."

### 必须由人做的五件事

1. **意愿** —— intent 里"问题是什么、为什么做"（agent 只能起草）
2. **取舍** —— spec 里策略冲突时选哪边（政策 owner 定）
3. **架构** —— plan 里"放弃哪些方案"（这是判断，不是生成）
4. **授权** —— 生产发布、受保护路径、迁移脚本
5. **归因** —— 事故根因结论与复盘定性

可以放手的：写代码、写测试、跑测试、定位 bug、整理变更日志、追调用链、起草文档、例行评审意见。

### 并行协作的边界

- 并行会话跑在各自 worktree，**彼此完全不知道对方存在**；唯一的共享状态是工程师
- subagent 提供**独立上下文**，让终检不被"产出代码的假设"污染
- **并行度上限不是机器，而是一个人能认真评审多少条流**（2–3 条起步）

### 防"橡皮章"的四个设计

| 风险 | 设计 |
|---|---|
| 人只看 AI 摘要就点批 | 评审内容限定为意图 + 风险；细节交给自动闸门 |
| 自审自批 | **写代码的 agent 无权批准**；批准权在 human code owner（分支保护）|
| 评审意见不可机器消费 | 严重度计数以机器可读形式发布到 check run，但门禁仍由人触发 |
| 治理空转 | 策略写成 skill（少违规），硬约束写成 hook（近乎不可能违规）|

---

## 6. 治理与安全控制

原则：**agent 可以做一切到生产闸门前，但不能越过它。**

### 托管设置示例（受监管企业）

由平台团队经 MDM / admin console 下发，工程师无法编辑或覆盖。

```json
{
  "permissions": {
    "deny": ["Read(.env*)", "Read(./secrets/**)", "WebFetch", "Bash(curl *)", "Bash(wget *)"],
    "allow": ["Bash(git *)", "Bash(make build)", "Bash(make test)", "Bash(make lint)"],
    "disableBypassPermissionsMode": "disable"
  },
  "allowManagedPermissionRulesOnly": true,
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "network": { "allowedDomains": ["git.internal.example.com", "registry.npmjs.org"] },
    "credentials": {
      "files": [
        { "path": "~/.ssh", "mode": "deny" },
        { "path": "~/.aws/credentials", "mode": "deny" }
      ],
      "envVars": [{ "name": "GITHUB_TOKEN", "mode": "deny" }]
    }
  },
  "allowManagedHooksOnly": true,
  "disableSideloadFlags": true,
  "allowManagedMcpServersOnly": true,
  "strictKnownMarketplaces": [{ "source": "github", "repo": "example-corp/approved-plugins" }],
  "requiredMinimumVersion": "2.1.193"
}
```

| 配置 | 控制意义 |
|---|---|
| `permissions.deny` | 密钥不进上下文；阻断工具层的任意出网 |
| `permissions.allow` | 预批准安全内环，避免 deny 列表变成"提示疲劳" |
| `disableBypassPermissionsMode` + `allowManagedPermissionRulesOnly` | 任何工程师、项目文件或命令行标志都无法放宽规则 |
| `sandbox` | 补上权限管不到的缝：工具层禁 `WebFetch` 挡不住 shell 出网，OS 级域名白名单才能彻底阻断 |
| `failIfUnavailable` / `allowUnsandboxedCommands` | 沙箱成为闸门：起不来就拒绝启动，沙箱内失败的命令不能到外面重试 |
| `credentials` | 补 `permissions.deny` 的缝：它只管 Claude 的文件工具，沙箱内 shell 仍可能读 `~/.ssh` |
| `allowManagedHooksOnly` | 只有本 play 的审批闸门会运行，本地无法新增或替换 |
| `disableSideloadFlags` + `strictKnownMarketplaces` | 每个 skill / agent / hook / MCP server 都来自组织批准的市场 |
| `allowManagedMcpServersOnly` | agent 的工具面是平台团队拥有的白名单 |
| `requiredMinimumVersion` | 低于批准版本下限拒绝启动，控制由已评估的构建强制 |

> 这是**起点而非可直接复制的推荐**。每条 deny 都在与能力做交换，具体平衡取决于仓库的数据分级。
> 参考：<https://code.claude.com/docs/en/settings>

---

## 7. 度量方式

每个 play 都带**领先指标 + 滞后指标**，共同逻辑是：领先指标看流程是否真的变快，滞后指标看守门是否失效。

| 阶段 | 领先指标 | 滞后指标 |
|---|---|---|
| Plan | 首次对话 → `intent.md` 提交时长 | intent 存活率；spec 首提后对 intent 的改动 |
| Design | intent → spec 的 commit 间隔 | build 开始后的需求返工 |
| Build（plan） | 一次实现即合并比例；计划批准 → 合并时长 | 每变更返工轮次；diff 与 plan 一致率 |
| Build（CLAUDE.md） | Claude 重复本应被拦住的错误的次数 | 新成员首个 PR 合并时长 |
| Build（skill） | 政策批准 → skill 合并时长 | 引用该政策的评审发现数 |
| Build（并行） | 单人并行会话数（评审质量不降） | 单人每周合并数 + 返工率 |
| Test（反馈回路） | 首次 CI 通过率 | 每 PR 评审时长；变更失败率 |
| Test（evals） | eval 通过率；事故 → 常驻 eval 时长 | CI 捕获回归 vs 生产发现回归 |
| Deploy（评审） | 首次评审时间；无人工介入即解决的评论占比 | 合并前捕获 vs 逃逸到生产的缺陷 |
| Deploy（hook） | 每个闸门的等待时长 | hook 前后逃到生产的闸门违规数 |
| Deploy（CI/CD） | 无需呼人即分诊的失败占比 | DORA 指标 |
| Maintain | 击穿 → 分诊队列出现 intent 的时长 | 变成合并修复的发现占比；同类事故复发率 |
| Maintain（扫描） | 计划扫描覆盖占比；发现 → 补丁进闸门时长 | 扫描发现 vs 生产发现；每轮发现数趋势 |

**红线**：合并量上升但变更失败率同步上升 → 收紧自主度，而不是加人。

---

## 8. 落地顺序与反模式

### 依赖关系（先做无入边的 play）

```
intent.md ──→ spec / design ──→ plan mode ──→ PR 评审 ──→ hooks 审批闸门 ──→ CI/CD ──→ 闭环
                    │              │
                    └── skills ────┴──→ 反馈回路 ──→ 持续 evals
CLAUDE.md（无前置，独立可做）
并行会话（需 CLAUDE.md + 反馈回路）
```

- **`CLAUDE.md`、`Capture as intent.md`、`Hooks`、`反馈回路` 基本无前置**，可以立刻开始
- `并行会话` 依赖 `CLAUDE.md` 与反馈回路（会话能自证，才敢少盯）
- `CI/CD 集成` 依赖 PR 评审 + 审批闸门（**闸门必须先存在，自动化才能加速穿越它**）

### 反模式

| 反模式 | 后果 | 对策 |
|---|---|---|
| 无测试就让 agent 大规模产码 | 技术债指数增长 | 反馈回路 + 无测试 PR 自动拒绝 |
| 人工逐行评审 AI 代码 | 评审成为新瓶颈，人疲于奔命 | 评审看意图与风险，细节交给自动闸门 |
| 只优化"写代码" | 瓶颈转移到 CI/评审后全面堵死 | 同步投资 CI、测试、评审工具 |
| 用 skill 当强制控制 | 政策"大致遵守"，审计时说不清 | 必须成立的政策配 hook 兜底 |
| 一次给超大任务 | 成功率低、返工贵 | 拆成可独立验证的小步 |
| 跑偏后硬修 | 上下文污染，越修越糟 | 回滚重来 |
| 无限追加 prompt 补丁 | 上下文膨胀、指令冲突 | 定期删减，用规则/测试替代文字 |
| 照搬大厂方案 | 成本远大于收益 | 先量自己的瓶颈在哪 |

### 语言无关的采纳节奏

1. **打地基**：`CLAUDE.md` + 单命令反馈回路；建立基线指标
2. **建闸门**：AI 评审 + 审批 hook + 无测试不合并
3. **建回路**：evals 跑配置变更；事故沉淀成 eval
4. **闭环**：确定性检测 + 分级响应 + intent 回流
5. **治理收口**：managed settings / 允许市场 / 最低版本

---

## 9. 模板附录

### `intent.md`

```markdown
# Intent: claims status self-service
Author: J. Ortiz (claims operations). Status: draft.

## Problem
Customers phone the contact center to ask where their claim is.
Handlers spend roughly a third of call time on status-only queries.

## Proposed outcome
Customers see claim status, next step and expected date in the portal.

## Affected users and systems
Claims handlers, portal team, claims-core API.

## Constraints
No new PII in the portal session. Existing authentication only.

## Open questions
Do third-party loss adjusters need access too?
```

### `spec.md` 的提示词

```markdown
Read the attached intent.md and produce a requirements and design spec for
integrating it into our existing codebase. Apply the skills available to you
so the plan conforms to our brand guidelines, security policies and UX
standards. Document the spec fully as spec.md, ready to hand to the
engineering team. Describe clearly any areas of concern, especially where
you cannot satisfy contradicting policies.
```

### `plan.md`

```markdown
# Plan: claims status self-service (from intent.md 2026-06-02)

## Files that change
portal/src/claims/StatusPanel.tsx (new), claims-api/routes/status.py,
claims-api/tests/test_status.py

## Order of work
1. Add the status endpoint behind existing auth.
2. Panel against the endpoint.
3. Wire into the portal nav.

## Risks
The claims-core API rate-limits at 50 rps; the panel must cache.

## Proof
test_status.py covers the four claim states; screenshot matches the
approved mock.
```

### 官方文档索引（按落地顺序）

| 主题 | 链接 |
|---|---|
| 组织级 Claude Code 设置决策图（从这里开始） | <https://code.claude.com/docs/en/admin-setup> |
| Settings 参考与优先级（含 managed-only 键） | <https://code.claude.com/docs/en/settings> |
| 服务端托管设置 | <https://code.claude.com/docs/en/server-managed-settings> |
| 权限 | <https://code.claude.com/docs/en/permissions> |
| 沙箱（OS 级文件与网络隔离） | <https://code.claude.com/docs/en/sandboxing> |
| Hooks 指南 / 参考 | <https://code.claude.com/docs/en/hooks-guide> · <https://code.claude.com/docs/en/hooks> |
| Skills | <https://code.claude.com/docs/en/skills> |
| Plugins 与私有市场 | <https://code.claude.com/docs/en/plugin-marketplaces> |
| Managed MCP（集中控制工具面） | <https://code.claude.com/docs/en/managed-mcp> |
| 企业部署（Bedrock / Vertex / Foundry） | <https://code.claude.com/docs/en/third-party-integrations> |
| 企业网络配置 | <https://code.claude.com/docs/en/network-config> |
| 监控（OpenTelemetry） | <https://code.claude.com/docs/en/monitoring-usage> |
| 分析仪表盘 | <https://code.claude.com/docs/en/analytics> |
| Compliance API | <https://platform.claude.com/docs/en/manage-claude/compliance-api> |

---

## 结语

> Models and harnesses have become more advanced, allowing organizations to not just transform how they produce code, but the entire software development lifecycle.
> **The loop keeps running. Human judgement stays above it.**

AI-native SDLC 的本质不是"用 AI 写代码"，而是**把工程团队的核心资产从代码转移到：工件链、测试、闸门和判断力**。
