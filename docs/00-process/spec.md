# Spec: VoiceIME M0（走通一次语音输入）

Status: draft → 待 G1 批准  
Source: `intent.md`（2026-10-28）  
Scope: **仅 M0**。GUI / 纠错 / 模型热切换不在本文件范围内（见 §9）。

> **技术路线变更（2026-10-29，D6）**：M0 交付改为 **Python + PyInstaller**，
> §3/§4 的 Rust + PyO3 目标态保留为后续可选方案；当前实现与验收以
> `spike/`、`02-guides/packaging.md` 为准。
>
> **后续落地（v0.23，2026-10-30）**：纠错已实现为「本地 Qwen2.5-0.5B + 守卫，默认关」
> （`correction.py`，G32），术语表方案已下线。本文件 §1/§9 仍按 M0 当初的范围书写，
> 现状以 `plan.md` 的 0.23 行为准。

---

## 1. 行为定义

一次输入 = 从热键按下到文字落入光标。

```
按下 Ctrl ──▶ 采集 16k/mono/i16 ──▶ 松开 Ctrl
                                        │
                                        ▼
                                  ASR（SenseVoice）
                                        │
                                        ▼
                             TextPostProcessor（清理）
                                        │
                          ┌─────────────┴─────────────┐
                          ▼                           ▼
                    空结果 → Dropped            非空 → Inject
                  （提示音，不注入）              （SendInput）
                          └─────────────┬─────────────┘
                                        ▼
                                  Trace 落盘 + 打印
```

M0 **不做**：VAD 切分、停顿换行、LLM 纠错、悬浮窗、GUI。

### 状态机

```
Idle ──hotkey down──▶ Recording ──hotkey up──▶ Decoding ──▶ Processing ──▶ Injecting ──▶ Idle
                          │                                  │
                        Esc                               空结果
                          ▼                                  ▼
                      Cancelled                            Dropped
```

约束：
- `Recording` 期间 MUST NOT 阻塞音频回调线程（只入队）
- 单次录音上限 60s，超出 MUST 截断并标记 `truncated=true`
- `Esc` 取消 MUST 不产生任何注入与提示音

---

## 2. 延迟预算（硬约束）

| 阶段 | 预算 | 说明 |
|---|---|---|
| 采集 → 松键触发 | 0ms | 松键即触发 |
| 音频规整（重采样/拼接） | <5ms | |
| ASR（5s 语音，8 核 CPU，int8） | ≤350ms | 实测空载 329/355ms、带载 485/648ms（8 线程，4.49s 音频） |
| 文本清理 | <1ms | 纯正则 |
| 注入（50 字，Unicode） | ≤10ms | |
| **M0 合计** | **≤400ms（不含未知项）** | 加 1.5s 余量充足 |

强制规则：
1. ASR 实测 >800ms 时 MUST 记录 `slow_asr=true`（M0 不降级；v0.23 起纠错默认关，
   未实现自动跳过——需要时由用户关掉开关，见 G32）
2. 任何新增处理 MUST 在本表登记额度
3. 首包冷启动 MUST 用 warmup 消除（1s 静音空跑一次）

---

## 3. 模块与职责

| 模块 | 语言 | 职责 | M0 是否实现 |
|---|---|---|---|
| `ime-audio` | Rust | cpal 采集、环形缓冲、重采样到 16k | ✅ |
| `ime-core` | Rust | 状态机、Trace、编排 | ✅ |
| `ime-infer` | Rust | PyO3 边界，调用 Python ASR | ✅ |
| `ime-post` | Rust | 幻觉过滤/语气词/归一化 | ✅ |
| `ime-inject` | Rust | 焦点快照 + SendInput | ✅ |
| `ime-hotkey` | Rust | 全局热键（rdev，Ctrl 长按 + Esc 取消） | ✅ |
| `ime-service` | Rust | 主进程，串联以上 | ✅ |
| `python/ime_infer` | Python | SenseVoice 推理 | ✅（含 mock 后端） |
| GUI | Tauri | 统计/配置 | ❌ M3 |
| 纠错 | Rust+Python | 本地 LLM + 守卫（默认关） | ❌ M2（Python 版已落地于 `correction.py`） |

---

## 4. 接口契约（G1 的核心）

### 4.1 Rust → Python

```python
# python/ime_infer/asr.py

def load(model_dir: str, threads: int = 4, backend: str = "auto") -> None:
    """加载模型并完成 warmup。重复调用是幂等的。"""

def transcribe(pcm: bytes, sample_rate: int = 16000) -> dict:
    """
    pcm: 小端 i16 单声道 PCM 原始字节
    返回: {"text": str, "lang": str, "rtf": float, "ms": int}
    失败: 抛 AsrError（Python 端），Rust 侧映射为 ImeError::Infer
    """
```

约束：
- **只传 bytes，不传文件路径**（避免 IO 与临时文件）
- `transcribe` MUST 是可重入的；内部串行化由 Rust 侧保证（单推理线程）
- 禁止在 Python 侧做重采样；采样率由 Rust 保证为 16000
- 返回的 `text` MAY 为空字符串（静音/噪声），**不是错误**

### 4.2 Python → Rust 错误映射

| Python 异常 | Rust `ImeError` | 用户可见行为 |
|---|---|---|
| `AsrError`（模型未加载/推理失败） | `Infer` | 提示音 + Trace.outcome=Failed |
| `ImportError`（缺依赖） | `InferNotReady` | 启动时即报错，不进入录音态 |
| 超时（Rust 侧 `recv_timeout`） | `Timeout` | 放弃本次，提示音 |

### 4.3 文本清理契约

```rust
pub fn process(raw: &str) -> Option<String>  // None = 不注入
```

规则（M0 子集，完整规则见 `../03-core-concepts/postprocess.md`）：
1. 幻觉黑名单整句命中 → `None`
2. 连续单字 >8 次折叠为 2 次
3. 句首/句中孤立语气词剔除（L1+L2）
4. 中英文之间补空格、连续标点去重
5. 空串 / 全标点 → `None`

### 4.4 注入契约

```rust
pub fn inject(text: &str, snap: &FocusSnapshot, cfg: &InjectConfig) -> Result<InjectOutcome>
```

- 默认策略 `Unicode`：`SendInput` + `KEYEVENTF_UNICODE`，按 UTF-16 码元发送，32 码元/块，块间 5ms
- 焦点已切换：
  - `restore_focus=true` → ALT 技巧恢复后注入
  - `restore_focus=false` → 放弃，返回 `FocusLost`
- 目标窗口提权而本进程非提权 → 返回 `ElevatedTarget`，**禁止静默失败**
- 换行 MUST 用 `VK_RETURN`，MUST NOT 发送 Unicode `\n`

---

## 5. 数据与存储

> 2026-10-29 更新（撤销偏离 D3）：用户要求"有数据的地方用 SQLite"，已落地
> `spike/voiceime_spike/store.py`，详见 `03-core-concepts/storage.md`。

- 配置：M1 落在 SQLite `settings` 表（`key/value`），不再引入 `config.toml`
- 历史/统计/设置：`%LOCALAPPDATA%/VoiceIME/voiceime.db`（WAL；
  Low 进程回退 `<spike>/.data`；冻结形态不可写时回退 `<exe 同级>/data`）
- Trace：仍可写 `trace.jsonl`（`--trace`），但 SQLite `history` 是主存储；
  **两者都不含音频**，文本受 `--no-store-text` 控制
- 模型：`<repo>/models/`，通过环境变量 `IME_MODELS_DIR` 覆盖

---

## 6. 非功能要求

| 项 | 要求 |
|---|---|
| 空闲内存 | < 800MB（模型常驻） |
| 空闲 CPU | < 1% |
| 启动时间 | < 5s（含模型加载 + warmup） |
| 崩溃隔离 | 音频线程 panic MUST NOT 杀死进程（catch_unwind + 上报） |
| 隐私 | 日志 MUST NOT 记录识别文本全文，只记长度 |

---

## 7. 验收（M0）

| ID | 项 | 判据 |
|---|---|---|
| A0-1 | 链路 | 记事本中连续 10 次输入，≥9 次正确 |
| A0-2 | 延迟 | 有真实 P50/P95 数字输出 |
| A0-3 | 清理 | 说"呃 今天 嗯 开会" → 输出不含"呃/嗯" |
| A0-4 | 空结果 | 静音 3s → 无注入、有提示音 |
| A0-5 | 取消 | 录音中按 Esc → 无注入 |
| A0-6 | 焦点 | 录音期间切到另一个窗口 → 按配置放弃或恢复 |
| A0-7 | 断网 | 断网后完整流程可用 |

---

## 8. 风险与对策

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| SenseVoice 在 8 核 CPU 上远超 350ms | 中 | 高 | M0 先实测；超标则缩短音频/换量化/上 GPU |
| 微信/Terminal 注入失败 | 中 | 高 | 剪贴板兜底 + 按窗口类切换策略 |
| PyO3 GIL 阻塞音频线程 | 低 | 高 | 推理独立线程，音频线程只入队 |
| 模型下载失败/过大 | 中 | 中 | 先支持本地目录 + mock 后端，模型后置 |
| 无 Rust 工具链 | 高 | 中 | 先用 Python spike 验证，再装 rustup |

---

## 9. 后续里程碑（不在本 spec）

| 里程碑 | 内容 |
|---|---|
| M1 | VAD 双阈值切分 + 无效段过滤 + 段重组 |
| M2 | LLM 纠错 + 守卫（已落地为默认关的本地 Qwen2.5-0.5B，见 G32；L0 规则纠错未采用） |
| M3 | Tauri GUI（统计/模型/配置/历史/关于）+ 模型下载热切换 |
| M4 | 打包（PyInstaller onedir，含托盘/自启；已提前验证基础打包，见 `02-guides/packaging.md`） |
