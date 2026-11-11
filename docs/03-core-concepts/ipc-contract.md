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
这样前端只需替换传输层，业务代码零改动。

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
