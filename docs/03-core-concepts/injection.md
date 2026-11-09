# 光标注入

把识别文本放进**当前光标位置**，不抢焦点。默认策略（`auto`）先走键盘，
确认目标没吃下按键时才动剪贴板。

实现基准：`spike/voiceime_spike/inject.py`；Rust 侧 `ime-inject` 必须等价。

## 三种策略

| 策略 | 机制 | 优点 | 缺点 | 何时用 |
|---|---|---|---|---|
| `unicode` | `SendInput` + `KEYEVENTF_UNICODE` | 不切输入法、不碰剪贴板、天然落 caret | 部分应用（DirectInput 游戏、少数终端）收不到 | 已知目标兼容，想省掉探测 |
| `clipboard` | 快照剪贴板 → 写入 → `Ctrl-V` → 恢复 | 兼容性最好 | 慢 500ms+，依赖目标支持粘贴 | 游戏/终端等不吃 `WM_CHAR` 的目标 |
| `auto` | 先 `unicode`；焦点控件**可读**且内容确实没变 → 再走剪贴板 | 正常情况下零开销，异常目标自动兜底 | 无法探测的目标（控制台/自绘）不能兜底 | ✅ 默认 |

**`auto` 的判据必须是"可探测"**：注入前后各读一次焦点控件的文本
（`GetGUIThreadInfo` → `WM_GETTEXTLENGTH`/`WM_GETTEXT`）。

- 文本变了 → 键盘策略成功，不碰剪贴板
- 文本没变 → 键盘被吞，改走剪贴板
- **读不到**（控制台、游戏、自绘控件）→ 只发键盘，不兜底

读不到就不兜底是刻意的：盲目兜底会把同一句话打两遍（先按键没反应、再粘一次）。
这类目标请显式 `--inject clipboard`。

## 剪贴板兜底（`clipboard` / `auto` 的降级路径）

1. 快照**整个剪贴板**：枚举 `EnumClipboardFormats`，凡是 HGLOBAL 型格式都把字节复制一份
   （文本、HTML、DIB、私有格式）；`CF_BITMAP` 等句柄型格式无法复制，会提示可能丢失
2. 写入待注入文本 → `SendInput` 发 `Ctrl+V`（事件同样带 `EXTRA_INFO_MAGIC`，PTT 钩子不会
   把自己的 Ctrl 当成用户按键，G20）
3. 等粘贴生效：可探测时轮询文本变化，之后仍等满 `clipboard_restore_ms`（默认 500ms，G3）
4. 恢复快照；但**如果剪贴板已被别的程序改写，就放弃恢复**，绝不覆盖用户刚复制的内容

恢复失败、剪贴板被占用都会打日志并返回 `Failed`，不静默。

## 焦点快照

注入前后都要知道"打给谁"：

| 字段 | 来源 | 用途 |
|---|---|---|
| `hwnd` | `GetForegroundWindow` | 判断焦点是否被切走 |
| `pid` | `GetWindowThreadProcessId` | 提权检测、日志 |
| `class_name` | `GetClassNameW` | 识别控制台类窗口（Terminal/WSL） |
| `elevated` | `OpenProcessToken` + `TokenElevation` | UIPI 判定 |
| `is_console` | 类名白名单 | 日志/诊断提示 |

快照时机：**录音开始时**。注入前重新取一次并比对 `hwnd`。

## 焦点丢失的处理

```
注入前 current.hwnd != snap.hwnd
  ├─ restore_focus=true  → ALT 技巧抢回，成功则继续注入，失败 → FocusLost
  └─ restore_focus=false → 直接放弃，返回 FocusLost
```

ALT 技巧：先模拟 `ALT` 按下/抬起再 `SetForegroundWindow`。直接调用受系统前台锁限制，会随机失败（G6）。

为什么默认放弃：把文字打进用户没想到的窗口（比如密码框之外的聊天窗口）比不注入更糟。

## 提权目标

`SendInput` 对提权窗口会被 UIPI **静默丢弃** —— 返回成功但没字（G7）。

规则：
1. 注入前 MUST 检测目标是否提权
2. 目标提权（`elevated=True`）+ 本进程非提权 → 返回 `ElevatedTarget`，**禁止静默失败**
3. 令牌读不到（`elevated=None`，受保护进程）→ 按普通窗口尝试注入，不武断拒绝（G13）
4. 提示语必须能指导用户（"以管理员身份运行 VoiceIME"）

## 码元切分（必须）

```
"😀"  →  U+D83D, U+DE00     （2 个 UTF-16 码元）
```

- 必须按 `encode_utf16()` 遍历，按字符（`chars()`）发送会把代理对拆坏 → 乱码
- 每批最多 **32 码元**，批间 **5ms**：一次性灌几千字会让部分应用丢字
- 换行/制表符 MUST 走 `VK_RETURN` / `VK_TAB`，**禁止**发 Unicode `\n`（部分应用只认虚拟键）

> 实测（2026-10-28，用户交互终端）：`chunk=32`（默认）与 `chunk=8` **均 5/5 通过**，
> 覆盖中文、中英混排、emoji 代理对、换行、184 字长文本。`CHUNK_UNITS = 32` 保持不变。
> 同进程 Tk 目标下注入耗时 32–255ms（不随字数线性增长，见 `docs/01-overview/latency-budget.md`）。

## 失败模式与对策

| 返回 | 触发 | 用户可见 | 对策 |
|---|---|---|---|
| `Injected` | 全部批次发送成功（`Trace.injection_strategy` 记录实际策略） | 出字 | — |
| `FocusLost` | 前台窗口变了且未开恢复 | 不出字 | 提示"焦点已切换" |
| `ElevatedTarget` | 目标提权、本进程普通权限 | 不出字 | 提示以管理员运行；剪贴板兜底也一样被 UIPI 拦住，必须显式失败 |
| `NoTarget` | 前台窗口为空（锁屏/桌面） | 不出字 | 忽略 |
| `Failed` | `SendInput` 返回 0、剪贴板被占用、可探测目标粘完仍无变化 | 不出字 | 记 `Trace.error`，日志给原因 |

## 验证方式

| 层级 | 手段 | 覆盖 |
|---|---|---|
| 单测 | `build_events` / `chunked` / `definitely_unchanged` / 假剪贴板 | 代理对、换行映射、分块、兜底判定、恢复/跳过恢复 |
| 手测 | 记事本、VSCode、浏览器输入框、微信、Windows Terminal | 真实兼容性（spec A0-1） |
| 自动 | `spike/inject_smoke.py`（Tk 窗口注入后读回比对） | 中文、emoji、混排、换行、长文本 |
| 跨进程 | Medium 进程起 WinForms `Edit`，另一个进程注入后读回 | 剪贴板路径 + 剪贴板恢复 + `auto` 不重复注入 |

手测矩阵 MUST 覆盖：中文、中英混排、emoji、换行、长文本（>500 字）、提权窗口、全屏游戏（预期失败）。

## 不变量

1. 注入文本 MUST 已经过 `postprocess`（空结果不进来）
2. 注入 MUST NOT 改变前台窗口（除非显式 `restore_focus`）
3. `unicode` / `auto` 的键盘路径 MUST NOT 触碰剪贴板
4. `clipboard` 路径 MUST 把剪贴板恢复成注入前的内容；用户在此期间复制了新内容则 MUST NOT 覆盖
5. 任何失败 MUST 有非空 `Trace.error`，且 `Trace.injection_strategy` 必须反映**实际**用过的策略
