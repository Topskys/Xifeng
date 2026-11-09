# 热键与 PTT

一次输入的起点。默认 **按住 Ctrl 说话，松开结束**。

实现基准：`spike/voiceime_spike/hotkey.py`；Rust 侧 `ime-hotkey`（rdev）必须等价。

## 为什么是"按住"而不是"点一下"

| 方式 | 起点明确 | 尾点检测 | 误触后果 | 选型 |
|---|---|---|---|---|
| 按住（PTT） | 按下即开始，零歧义 | 不需要，松开即结束 | 误按 → 一次极短录音，被丢弃 | ✅ 默认 |
| 点一下（toggle） | 需要额外确认 | 靠 VAD 静音 240ms | 忘关 → 录到整段环境音 | M1 可选 |

PTT 的价值不是"更酷"，而是**没有尾点检测开销**：e2e 里省掉 240ms 的等待。

## 键位

默认 `ctrl` —— **左 Ctrl 或右 Ctrl 任意一个**。

| 键 | 值 | 说明 |
|---|---|---|
| `ctrl` | VK_LCONTROL 0xA2 / VK_RCONTROL 0xA3 | 默认 |
| `lctrl` / `rctrl` | 单侧 | 想避免与快捷键冲突时选 `rctrl` |
| `shift` / `lshift` / `rshift` | 0xA0 / 0xA1 | 备选（右 Shift 几乎不与快捷键冲突） |

### Ctrl 的代价

Ctrl 是高频修饰键，任何 `Ctrl+C` / `Ctrl+V` 都会产生一次"按下-松开"。
若不做保护，每次复制粘贴都会触发一次录音并往光标里注入噪声。

保护措施：**`min_hold_ms = 250`** —— 按住不足 250ms 直接丢弃，不进入 ASR。

> 250ms 是取舍：正常说话一句至少 500ms，够短的口令（"好"）也在 300ms 以上。
> 想更宽松可以 `--min-hold-ms 150`，代价是 `Ctrl+C` 可能漏进来。

## 检测方式

| 方案 | 能拿到 KeyUp | 需要消息泵 | 是否偷按键 | 选型 |
|---|---|---|---|---|
| `RegisterHotKey` | ❌ 只有按下 | ❌ | 会从目标窗口拿走该组合 | 不可用 |
| `GetAsyncKeyState` 轮询 | ✅ | ❌ | ❌ 仅观察 | 兜底（本机 shell 读不到，见 G21） |
| `WH_KEYBOARD_LL` 低级钩子 | ✅ | ✅ 必须 | ❌ 仅观察 | ✅ 当前默认 |

钩子跑在独立守护线程的 `GetMessageW` 循环里，回调只维护"当前按下集合"，不做耗时操作。
安装时 `hMod` 必须传 `NULL`（LL 钩子的回调在本进程内执行），传 EXE 句柄会失败（G19）。

轮询作为兜底保留：钩子装不上时自动退回，`--diagnose-keys` 会打印当前用的是哪种。

采集延迟：钩子是事件驱动，**松开即时感知**；轮询模式下延迟 ≤ `poll_ms`（10ms）。

### 只忽略自己的注入

钩子会收到本程序注入的文字。判据不是 `LLKHF_INJECTED`（那会把键盘重映射软件的
真实按键也一起忽略，见 G20），而是 `dwExtraInfo`：

- `inject.py` 给所有事件打 `EXTRA_INFO_MAGIC`（`'VIME'`）
- 钩子读到同样的 `dwExtraInfo` 才跳过，其它事件一律照收

这样"注入不触发 PTT"和"重映射键盘能用"同时成立。

### 读不到键时先诊断

`SendInput` 自测键状态不可靠（G18），必须让真人按。两个层次的诊断：

```bash
uv run python run_spike.py --diagnose-keys 20   # 只看按键，秒级
uv run python key_diag.py                       # 全面：环境 + 钩子两种线程模型 + Tk 判据
```

`run_spike.py --diagnose-keys` 的输出判读：

| 现象 | 结论 |
|---|---|
| 全部能检测到 | 键读取正常，问题在别处（键位/误触门槛） |
| 字母能检测到、Ctrl 不行 | 有软件改写了 Ctrl 映射（键盘驱动/快捷键工具） |
| 心跳显示"收到 0 个事件" | 钩子收不到事件（G19）；试管理员运行或换键 |
| 心跳显示事件数 > 0 且"注入"占多数 | 键盘经过重映射软件（G20），不影响使用 |
| 采集方式=polling 且一个都没有 | 钩子没装上且轮询也读不到 → 看 `key_diag.py` |

`key_diag.py` 会打印**完整性级别**。如果看到 `Low(0x1000)`，任何全局钩子方案都读不到 ——
这是环境限制，不是代码问题（G21）。正常桌面应为 `Medium(0x2000)`。

本工作区（`D:\project\workspace`）带 Low 完整性标签，目录内的 exe 全都是 Low。
进程完整性只由 exe 决定、与 CWD 无关，所以新建一个目录外的 venv 就能绕开：

```bash
uv venv %TEMP%\vi-venv --python 3.11
set PYTHONPATH=.venv\Lib\site-packages
%TEMP%\vi-venv\Scripts\python.exe key_diag.py          # 应为 Medium(0x2000)
%TEMP%\vi-venv\Scripts\python.exe ptt_selftest.py      # PTT 时序自测（4 个用例）
```

`key_diag.py` 第 1b 节会自己发一个 F13，判断钩子回调是否被调用 —— 不需要人按键
就能分清"钩子坏了"和"按键被上游过滤"。

它还有第 5 步：在 Tk 窗口里**按住 Ctrl 再松开**，量真实按住时长，用来验证
`min_hold_ms` 门槛与"松开即结束"的语义。窗口消息不受完整性限制，所以这一步在
Low 环境里也能做 —— 是唯一能在沙箱内验证 PTT 时序的手段。

## 时序

```
等待 ──▶ 麦克风关闭（不占设备）
  │
按下 Ctrl ──▶ start()：开流（100–400ms 静默期，G23）──▶ begin()
  │                                              │
  │                                              ├─ 每 10ms：Esc? 松开? 超时?
  ▼                                              │
松开 Ctrl ──▶ 再收 drain_ms=120 ──▶ end() 取音频 ──▶ stop()/close()
                                     ──▶ ASR ──▶ 清理 ──▶ 注入
```

### 为什么默认按需开麦、以及它挡住了什么

用户要求"不录音就不占麦克风"（Windows 麦克风指示不能常亮），所以默认
**按下才开流、松开就关流**。代价是开流本身有 95–376ms 的静默期
（`blocksize` 决定，见 `docs/05-gotchas.md` G23）：这段音频不存在，抢话会丢句首。
所以交互上先显示"录音中"，用户看到提示再开口。

| 段 | 参数 | 补什么 | 不补的后果 |
|---|---|---|---|
| 开流+前置 | `pre_ms=300` | 开流后到 `begin()` 之间已到达的头几块 | 句首一两个字消失 |
| 松手收尾 | `drain_ms=120` | 宿主输入缓冲里还没回调的音频 | 最后一个字/音节被截 |
| 常驻（可选） | `hotkey.start_mic()` / `--resident-mic` | 把静默期付在启动时，而不是按键时 | 每句开头都要丢 0.1–0.4s |

`pcm` 长度 = 开流后头几块 + 按住时长 + `drain_ms` + 回调抖动；`PttResult.seconds`
只算按住时长（不含 pre/drain），`min_hold_ms` 判定用它。只有显式
`--resident-mic` 才会在启动时开流并常驻占麦，`record_ptt` 检测到常驻流就直接复用。

| 参数 | 默认 | 作用 |
|---|---|---|
| `poll_ms` | 10 | 键状态采样周期，决定松开检测延迟 |
| `min_hold_ms` | 250 | 低于此值判误触，丢弃且不产生 Trace 的 Injected |
| `max_seconds` | 60 | 单次上限，超出截断并置 `truncated=true` |
| `pre_ms` | 300 | 前置缓冲长度，补偿开流静默期 |
| `drain_ms` | 120 | 松手后继续收流的时长，捞回尾音（取消时不收） |

## 取消

录音中按 **Esc** → 丢弃本次音频，`outcome=Cancelled`，不注入、不提示。

Esc 与 PTT 键共用同一采集源：钩子可用时读钩子维护的按下集合，否则退回 `GetAsyncKeyState`（`hotkey.pressed()` 统一分派）。

## 焦点快照时机（重要偏差）

设计上 MUST 在**按下热键的瞬间**快照前台窗口（`docs/03-core-concepts/injection.md`），
因为用户可能在说话期间切窗口。

spike 当前在**注入前**才快照 —— 属已知偏差，等价于"只检查松手瞬间的焦点"。
Rust 侧 MUST 改回按下时快照，否则"按住说话时误切窗口"会被判成焦点正常。

## 已知风险

1. **Ctrl 与 IDE/浏览器快捷键冲突**：全屏应用里按住 Ctrl 时滚轮会缩放。属可接受噪声。
2. **输入法切换**：`Ctrl+Space` 会切输入法。按住 Ctrl 说话期间按了 Space 也没关系 —— 监听只观察状态、不吞键，录音继续；松开 Ctrl 才结束。真正受影响的是"短按 Ctrl 顺带切了输入法"的情况，与 PTT 本身无关。
3. **粘滞键**：Windows 粘滞键开启时 Ctrl 会"锁住"，导致不按也一直录音。M1 需检测 `GetKeyState(VK_CONTROL)` 与实际物理状态差异，或在设置里提示。
4. **游戏/全屏**：部分游戏独占键盘，`GetAsyncKeyState` 仍可读，但注入会被拦（见 `docs/03-core-concepts/injection.md`）。

## 不变量

1. 松开检测延迟 ≤ `poll_ms`，MUST NOT 截掉句尾；松手后 MUST 收满 `drain_ms`
2. 按住时间 < `min_hold_ms` → MUST 丢弃，MUST NOT 进入 ASR
3. Esc MUST 丢弃且 MUST NOT 注入
4. 单次录音 MUST ≤ `max_seconds`，超出 MUST 截断并标记
5. 热键监听 MUST NOT 抢走按键（只观察，不吞）
