# Trace 与统计口径

本文件定义一次输入的可观测数据。**字段名即契约**：Python 核、Rust 侧序列化、GUI 统计页三者必须一致。

## 一次输入 = 一条 Trace

```json
{"id":12,"ts":"2026-10-28T22:31:04.512+08:00","mode":"ptt","outcome":"Injected",
 "ms_asr":312,"ms_post":0,"ms_correct":0,"ms_inject":9,"e2e_ms":338,
 "chars":22,"speech_ms":3120,"truncated":false,"dropped_segments":0,
 "injection_strategy":"unicode","error":""}
```

## 字段

| 字段 | 类型 | 含义 | 约束 |
|---|---|---|---|
| `id` | int | 进程内自增序号 | 重启后从 1 开始，不作主键 |
| `ts` | string | 记录结束时刻（本地时区，毫秒精度） | ISO 8601 |
| `mode` | string | `ptt` / `vad` | M0 恒为 `ptt` |
| `outcome` | enum | `Injected` / `Cancelled` / `Dropped` / `Failed` | 见下 |
| `ms_asr` | int | ASR 调用耗时 | 含 Python 边界开销 |
| `ms_vad` | int | Silero VAD 前向耗时（仅 `use_vad=True`；PTT 能量裁剪记在 `ms_trim`） | M1 起 |
| `ms_post` | int | 文本清理（+纠错）耗时 | 纠错单独记 `ms_correct` |
| `ms_correct` | int | 模型纠错耗时（`model_correct=True` 时） | 默认 0；守卫拒绝也照记 |
| `ms_inject` | int | 注入耗时 | 含块间 5ms 间隔 |
| `e2e_ms` | int | 热键松开 → 注入完成 | **SLO 依据** |
| `chars` | int | 注入字符数 | **禁止**记录文本本身 |
| `fixes` | int | 纠错采纳的修改处数 | 只有数字；默认 0（纠错关闭） |
| `speech_ms` | int | 输入音频时长 | 用于算 RTF |
| `truncated` | bool | 是否因 60s 上限被截断 | M0 未启用 |
| `dropped_segments` | int | 被 VAD 丢弃的段数 | M0 恒 0 |
| `injection_strategy` | string | `unicode` / `clipboard` / `none` | 记录**实际**用过的策略：`auto` 兜底成功时写 `clipboard`；`none` = dry-run，未真的发键 |
| `error` | string | 失败原因（≤120 字符） | 空串表示无错误 |

> `ms_correct` 只出现在 Trace / JSONL；SQLite `history` 表把纠错时间并入 `ms_post`
> （schema v3 没有该列，避免为统计再升一次版本）。

## outcome 语义

| outcome | 何时产生 | 用户感知 |
|---|---|---|
| `Injected` | 文本非空且注入成功 | 出字 |
| `Dropped` | 空结果 / 幻觉 / 全语气词 | 静默（建议轻提示音，M1） |
| `Cancelled` | 录音中按 Esc | 静默 |
| `Failed` | ASR 异常、注入失败、超时 | 提示音 + 可查错误 |

规则：
1. 任何 `Failed` MUST 带非空 `error`
2. `Dropped` MUST NOT 被当作失败统计（会污染失败率）
3. `e2e_ms` 只在 `Injected` 上参与 SLO 统计

## 采样与存储

| 阶段 | 做法 |
|---|---|
| M0（Python 核） | JSONL 逐行追加，默认关闭，`--trace path` 打开 |
| M1（当前） | **SQLite**（`history/settings`）+ 可选 JSONL；默认开，`--no-db` 关 |
| M3 | GUI 读同一个库，保留期可配 |

**隐私**：Trace MUST NOT 含识别文本全文；历史表是用户可见数据（`--no-store-text` 可只存长度），
音频从不落库。详见 `03-core-concepts/storage.md`。

## 识别质量（CER）

延迟达标不代表能用。质量用**字错率**衡量，不靠"听着还行"：

```
CER = 编辑距离(参考文本, 识别文本) / len(参考文本)
```

比对前先归一化：**去标点、去空白、转小写**，只比"字"。归一化的理由 —— 标点是 ASR 的附带产物，用它算错没有意义。

| 指标 | 含义 | 谁算 |
|---|---|---|
| `CER` | 单句字错率 | `core/quality_check.py` |
| 平均 CER | 5 句固定文本的算术平均 | 同上 |
| 完全正确句数 | CER = 0 的句子数 | 同上 |

判据（本项目自定）：

| CER | 判定 | 处理 |
|---|---|---|
| ≤ 5% | 合格 | — |
| 5%–15% | 可接受 | 记录错例，评估是否进 M2 纠错 |
| > 15% | 有问题 | 先查采集链路（电平/设备/噪声），再谈纠错 |

约束：

1. **必须用真人录音**：TTS 合成音频对 SenseVoice 太友好，CER 接近 0，不能代表真实分布
2. **必须固定句子**：换句子就不可比，句子表在 `quality_check.py` 的 `SENTENCES`
3. **语言 MUST 固定 `zh`**：`auto` 在本机把普通话判成粤语（G22），CER 会从 3% 飙到 25%
4. **ITN 默认关闭**（G12）：它把"一百五十"写成"150"（表面形式，正文里无法还原），
   且混排英文明显退化（句3 CER 4.5% → 27.3%）。
   ⚠ 曾以为"数字改写交给 postprocess 规则"，但 `postprocess.py` / `crates/ime-post`
   里**并没有**中文数字→阿拉伯数字的规则（`normalize` 只做空格/标点/空白规整）：
   关掉 ITN 后"九点"就原样注入。要改数字形态必须先补规则（两边同步 + 跑 parity），
   见 `docs/05-gotchas.md` G45
5. 录音留在 `core/fixtures/quality/`，可事后用 `--wav-dir` 复算同一批次

### 实测（2026-10-29，5 句真人录音，4.6–8.0s）

| 配置 | 平均 CER | 完全正确 | 说明 |
|---|---|---|---|
| auto + ITN（初版） | 24.5% | 0/5 | 句1 被判成粤语（57%），ITN 改写数字 |
| zh + ITN | 15.5% | — | 语言修好，ITN 仍伤混排英文 |
| auto + 无 ITN | 17.4% | — | 语言问题还在 |
| **zh + 无 ITN（当前）** | **3.3%** | **3/5** | 合格 |

### 纠错效果（v0.23：本地小模型，默认关）

`quality_check.py` 默认量**注入前的最终文本**（清理 → 归一化），`--no-refine` 才只看
ASR 原始输出，`--correct` 才把模型纠错计入（与运行时默认一致）：

| 配置 | 平均 CER | 完全正确 | 备注 |
|---|---|---|---|
| ASR 原始输出（`--no-refine`） | 3.3% | 3/5 | 句3 4.5%、句5 11.8% |
| 清理 + 归一化（`--refine`，默认） | ~3.3% | 3/5 | 规则层下线后回到基线，见下 |
| + 模型纠错（`--correct`） | 取决于守卫放行率 | — | 模型输出不稳定，**不承诺**固定收益 |

原位规则层（术语表 + 复读折叠）在 2026-10-29 曾把 CER 压到 0.0%（5/5），
但它的代价是"每加一条规则都可能误伤"，且换一种错法就失效；v0.23 起整体下线，
改由本地 Qwen2.5-0.5B 承担（`correction.py`，G32）。

模型侧实测（8 线程，热身后）：

| 输入 | 模型输出 | 守卫裁决 |
|---|---|---|
| `这个会议机要发给我` | `这个会议纪要发给我。` | ✅ 采纳（同音字） |
| `请把 red me 文件发给张三` | `请把 README 文件发给张三` | ✅ 采纳（专有名词） |
| `帮我把 voice i e 的 read me 提交` | `把Xifeng的README提交` | ❌ `drop-start`（吞掉"帮我"） |
| `今天的会推迟到下午三点` | `今天的会推迟到下午三点二十。` | ❌ `edge-add`（句尾续写） |
| `这个会议机要发给我` | 原样 / 补句号 | ❌ `same` / `punct-only` |

**所以纠错默认关**：0.5B 的输出不稳定（同 prompt 多次结果不同，`temperature=0` 也不保证），
守卫只能保证"不改坏"，不能保证"改对"。开启后每句 +0.35–1.5s，可能破 P95。

剩余错例（规则层下线后仍存在，作为模型纠错的回归样本）：

| 句 | 参考 → 识别 | 性质 | 处理 |
|---|---|---|---|
| 3 | `Xifeng` → `voice i e`、`README` → `read me` | 中英混排的英文词被拆成音节，属模型能力边界 | 模型纠错（开启时） |
| 5 | `就提交` → `就交就提交` | 复读式重复插入（偶发，每次形态可能不同） | 模型纠错（开启时）；守卫允许 ≤3 字折叠 |

> 采集链路无问题：句1/2/4 在正确配置下 CER = 0，说明音频与麦克风是干净的，
> 剩下的偏差全部来自模型与配置，不是录音。

> **耗时波动**：同一批音频连跑三次，单句 ASR 在 274–1023ms 之间波动（P50 408–601ms），
> 期间本机在跑构建与测试。引用 ASR 数字 MUST 注明 CPU 占用；SLO 只认空载 P95。

## 统计口径

- 分位数用**最近秩法**：`p95 = 排序后第 ceil(0.95*n) 个`，空集返回 0
- SLO 只看 `outcome=Injected` 的 `e2e_ms`，P95 ≤ 1500ms
- RTF = `ms_asr / speech_ms`；ASR > 800ms 时 SHOULD 关闭模型纠错（当前实现未自动降级，
  靠默认关 + 用户开关；若开启且 ASR 偏慢，e2e 可能破 1.5s，见 `latency-budget.md`）

## 对应实现

| 位置 | 文件 |
|---|---|
| Python 核 Trace | `core/xifeng/trace.py` |
| Python 核质量评估 | `core/quality_check.py` |
| 历史 / 统计 | `core/xifeng/store.py`（`history` 表） |
| 规则层（已下线） | 原 `core/xifeng/glossary.py`；现由 `correction.py` 取代 |
| Rust | `crates/ime-core/src/trace.rs`（推迟，见 D6） |
