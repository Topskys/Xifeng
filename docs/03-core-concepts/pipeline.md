# 管线与状态机

一次输入从按键到出字，经过哪些状态、在哪里打点、出错往哪走。

实现基准：`spike/xifeng/pipeline.py`（M0 顺序链路）；Rust 侧 `crates/ime-core`。

## 状态机

```
        hotkey down
Idle ──────────────▶ Recording ──hotkey up──▶ Decoding ──▶ Processing ──▶ Injecting ──▶ Idle
                       │                          │             │
                      Esc                       空结果       清理后为空
                       ▼                          ▼             ▼
                   Cancelled                   Dropped       Dropped
                       │                          │
                       └────────── Failed ◀──────┘（ASR 异常 / 注入失败 / 超时）
```

| 状态 | 含义 | 允许的操作 |
|---|---|---|
| `Idle` | 等待热键 | 只接受热键按下 |
| `Recording` | 采集音频 | 只入队；Esc 取消；60s 上限 |
| `Decoding` | ASR 推理 | 不可取消（M0），超时 5s |
| `Processing` | 文本清理 +（可选）模型纠错 | 纯内存，不可中断 |
| `Injecting` | SendInput 序列 | 不可取消，块间 5ms |
| 终态 | `Cancelled` / `Dropped` / `Failed` | 落 Trace 后回 `Idle` |

强制规则：
1. `Recording` 期间 MUST NOT 阻塞音频回调线程（只入队）
2. 单次录音上限 60s，超出 MUST 截断并置 `truncated=true`
3. `Esc` 取消 MUST 不产生注入、不产生提示音
4. `Decoding` 超时 MUST 放弃本次并置 `Failed error=Timeout`

## PTT 与 VAD 两种模式

| 模式 | 起点 | 终点 | e2e 组成 |
|---|---|---|---|
| PTT（M0） | 按下热键 | 松开热键 | ASR + 清理 (+纠错) + 注入 |
| VAD（M1） | 检测到说话 | 静音 240ms | 尾点 240ms + ASR + 清理 + 注入 |

M0 用 PTT：**没有尾点检测开销**，延迟预算最省，也最容易定位问题。VAD 的价值是"不用按住"，不是更快。

## 打点位置

| 打点 | 起点 | 终点 | 写入字段 |
|---|---|---|---|
| e2e | 热键松开（或开始录音） | 注入完成 | `e2e_ms` |
| ASR | 进入 `Decoding` | `transcribe` 返回 | `ms_asr` |
| 清理 | 进入 `Processing` | `process`（+纠错）返回 | `ms_post`（纠错另记 `ms_correct`） |
| 注入 | 进入 `Injecting` | 最后一批 SendInput 返回 | `ms_inject` |

口径与字段定义见 `../04-reference/metrics.md`。

## 文本层顺序

```
raw(ASR) → postprocess.process →（model_correct?）correction.correct → check 守卫
                                        │                    │
                                        │                    └─ 拒绝 → 保留清理后的原文
                                        └─ 采纳 → normalize → inject
```

- 纠错在 `postprocess` **之后**：先去语气词/幻觉，模型看到的是干净句子，误判更少
- 纠错**默认关**（`PipelineConfig.model_correct=False`）。开启时 MUST 在 `Pipeline.warmup()`
  里先 `correction.warmup()`，否则第一句 +4s（G32）
- 守卫是唯一安全边界：句首删字 / 连续删 >3 字 / 句首句尾新增 / 只改标点 / 解释性输出 /
  相似度 <0.80 一律回退原文。**宁可漏改，不能改坏**
- `trace.fixes` 记采纳的修改处数，`trace.ms_correct` 记耗时（只有数字，不含文本）
- 模型缺失/加载失败只警告一次并关闭纠错，之后按原文注入，不逐句重试

## 空结果路径

```
ASR → ""            → Dropped（静音/噪声）
ASR → 幻觉句        → Dropped（G10）
ASR → 只有语气词    → Dropped（standalone=drop）
清理后剩纯标点      → Dropped
```

**Dropped 不是错误**，不进失败率。区分它们的唯一原因是：Dropped 说明"用户没说话或说了废话"，Failed 说明"我们的链路坏了"。

## 线程模型（Rust 侧）

```
主线程(tokio)   状态机、Trace、日志
音频线程        cpal 回调：只入队，禁止分配/加锁/IO/调 Python
推理线程        Python 解释器所在线程，唯一持 GIL 者
注入线程        SendInput 批次（块间 5ms）
```

约束：Python 解释器启动时在推理线程初始化一次，之后 MUST NOT 迁移线程。

## M0 与目标态的差异

| 项 | M0（spike） | 目标态 |
|---|---|---|
| 触发 | 回车 / 固定秒数 / Ctrl 长按 | 全局热键（rdev，Ctrl 长按） |
| 取消 | 无 | Esc |
| 音频缓冲 | `list.append` | 预分配环形缓冲 |
| VAD | 能量阈值（可选） | Silero v5（30ms 块） |
| 纠错 | 本地模型（默认关，G32） | LLM 纠错 + 守卫（M2） |
| Trace | JSONL | 环形缓冲 + SQLite（M3） |
