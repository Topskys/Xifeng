# IPC 契约（Tauri ↔ Python 核心）

> 状态：**冻结**（2026-11-09）。三方实现（Rust 壳 / Python 核心 / 前端桥）MUST 遵循本文，
> 任何改协议先改这里，三处同步后再提交。

## 传输

- Tauri 用 `std::process::Command` 启动 Python 子进程：
  `<python> run_spike.py --ipc --db <path>`
- 走 **stdin/stdout**，**行分隔 JSON**（NDJSON）。一行一条消息，UTF-8，无 BOM。
- **stdout 是协议专用通道**：Python 侧所有日志/print MUST 走 stderr。
  任何误写 stdout 的东西都会破坏协议（这是本设计唯一的硬红线）。

## 消息格式

请求（Tauri → Python）：

```json
{"id": 1, "method": "bootstrap", "params": {}}
```

响应（Python → Tauri），必须带同一个 `id`：

```json
{"id": 1, "ok": true, "result": {"version": "0.1.0", ...}}
{"id": 1, "ok": false, "error": "数据库打开失败"}
```

通知（Python → Tauri，无 `id`，用于主动推送，可选）：

```json
{"event": "ready", "result": {"pid": 1234}}
{"event": "log", "result": {"level": "info", "msg": "..."}}
```

## 方法表

与现有 HTTP 路由一一对应（`webgui.py` 的 `Api` 方法），参数与返回值**完全一致**，
这样前端只需替换传输层，业务代码零改动。（共 14 个；D16 追加的 4 个推理方法见下一节。）

| method | 参数 | 返回 |
|---|---|---|
| `bootstrap` | — | `Bootstrap`（见 api.ts） |
| `stats` | `{"period": "7d"}` | `StatsPayload` |
| `history` | `{"period", "offset", "limit"}` | `{rows, has_more}` |
| `history_delete` | `{"id": 1}` | `{ok: true}` |
| `settings_save` | `Partial<Settings>` | `Settings` |
| `settings_reset` | — | `Settings` |
| `theme` | `{"theme": "dark"}` | `Settings` |
| `models_use` | `{"path": "..."}` | `{current, list}` |
| `models_find` | `{"name", "files"}` | `{matches: []}` |
| `autostart` | `{"enabled": true}` | `{ok, error}` |
| `prune` | — | `{removed: n}` |
| `open` | `{"path": "__data__"}` | `{ok: true}` |
| `check_update` | — | `{latest, current, update}` 或 `{error}` |
| `shutdown` | — | `{ok: true}`，随后进程退出 |

### D16 运行时方法（2026-11-13 追加；correct/session_record 为 2026-11-14 P2 追加）

Rust 壳接管热键/采集/注入后（决策 D16，docs/00-process/plan.md），
Python 核心退化为模型运行时。它们**不经过 `webgui.Api`**，
由 `xifeng/asr_runtime.py` 的 `AsrRuntime` 实现；模型延迟加载 + 常驻，
`ready` 之后由后台线程 warmup（绝不阻塞 ready 与协议主循环），
首次 `asr_begin` 一般不再等 2~4s 冷启动。

| method | 参数 | 返回 |
|---|---|---|
| `asr_begin` | `{}` | `{}`。清空 utterance 缓冲；确保 ASR 模型已加载（首次调用会加载，耗时 2~4s，调用方容忍 30s 超时） |
| `asr_feed` | `{"pcm_b64": "..."}` | `{frames: n}`。追加 16kHz 单声道 **i16 LE 裸字节**（base64 编码，容忍换行）；返回累计帧数（=已接受字节数/2）。缓冲上限 60s（约 960000 帧），超出丢弃新数据并计一次警告日志 |
| `asr_end` | `{}` | `{text, chars, ms}`。对缓冲做与 ptt_loop 相同的静音裁剪/VAD + ASR 解码；**返回原始输出，未经 postprocess**（空串合法）；`ms`=解码耗时；返回后清空缓冲 |
| `asr_cancel` | `{}` | `{}`。丢弃缓冲（Esc 取消本次） |
| `correct` | `{"text": "..."}` | `{applied, text, fixes, reason, ms}`。本地 LLM 纠错（`model_correct` 开关在壳侧判断，Python 侧总可用）；**G32 守卫在 `correction.check` 内**——`applied=false` 即拒绝，`text` 恒为原文，壳只在 `applied=true` 时采用；首调用付模型加载（失败粘性，绝不逐句重试）；持 `_load_lock` 与 ASR 解码互斥 |
| `session_record` | Trace 字段的壳侧子集：`{outcome, chars, e2e_ms, ms_asr, ms_post, ms_inject, speech_ms, strategy, fixes, error, text, raw_text, clean_text}`（全部可省略） | `{id}`。写一条历史（mode=ptt）；`store_text` 关闭时文本不落库（开关在 `add_history` 内生效）；数据库/参数问题绝不抛出——返回 `{"id": -1}`，不影响已完成的注入 |

约束：

- 裁剪/VAD 配置来自用户设置与命令行（`--trim`/`--vad` 语义不变）；
  注入策略等与推理无关的配置在此路径忽略（`model_correct` 除外——它决定
  warmup 是否预热 LLM 与 `correct` 是否可用）
- ASR 加载/推理失败（`AsrError`）→ `ok=false` + 简短 error，不崩进程；
  纠错失败/守卫拒绝**不是**错误：`correct` 恒 `ok=true`，看 `applied`
- 日志只记长度与耗时，**禁止记录识别文本全文**（仓库硬约束）
- `asr_feed` 的 pcm 必须是偶数字节长度；奇数长度/非法 base64 → `ok=false`

## 生命周期

1. Tauri 启动 → spawn Python → 等第一条 `{"event":"ready"}`（超时 30s 报错；2026-11-11 从 10s 放宽——统一根有模型后，冷盘首启要把 228MB ASR 模型读进内存）
2. 前端就绪 → 发 `bootstrap` 拿全量状态
3. Tauri 退出 → 先发 `shutdown`，再 `child.kill()` 兜底（防僵尸进程）
4. Python 异常退出 → Tauri 转发 `{"event":"exit","result":{"code":N}}`，前端显示横幅

## 错误处理

- 未知 method → `{"ok": false, "error": "unknown method: xxx"}`
- 参数错误 → `{"ok": false, "error": "..."}`，**不崩进程**
- 单条消息处理异常 → 捕获并回 error，继续处理下一条
- 只有 stdin EOF 或 `shutdown` 才退出主循环

## 兼容性

未启用 `--ipc` 时，`run_spike.py --web` 行为完全不变（HTTP 模式保留，
用于 `--tray` 场景和调试）。前端检测到 `window.__TAURI__` 走 IPC，否则走 fetch。
