# 坑位清单

改相关模块前必读。每条含触发条件与对策。

## G1 PyO3 / GIL

- Python 解释器 MUST 固定在推理线程初始化，禁止跨线程使用
- GUI 与核心分进程是刻意的：Python 崩溃（如 llama.cpp segfault）MUST NOT 拖死输入功能
- 推理线程持 GIL 时，Rust 侧不得再调用 Python API（会死锁）

## G2 UTF-16 代理对

- `SendInput` 按 UTF-16 码元发送。emoji（😀 = 2 码元）按字符发送会乱码
- 用 `text.encode_utf16()`，**禁止** `chars()`

## G3 剪贴板恢复时序

- `Ctrl-V` MUST 在 `SetClipboardData` 完成后 ≥30ms 发出，否则粘贴旧内容
- 恢复剪贴板 MUST 延迟 ≥500ms（`--clipboard-restore-ms`），否则慢应用读到的还是原内容
- 能探测焦点控件文本时，先轮询"粘贴已生效"再等满 500ms（`_wait_paste_landed`）
- 恢复前 MUST 确认剪贴板里还是**我们写入的文本**；已被别的程序改写就跳过恢复，
  绝不覆盖用户刚复制的内容

## G4 VAD 段边界

- 丢弃段两侧 MUST 留 60ms 缓冲，否则字头爆破音被切掉导致误识别

## G5 语气词正则

- 中文填充词清理 MUST 仅作用于含 CJK 的输出（英文里 `er` / `um` 是正常词）
- 删除位置 MUST 满足 `(^|[，。？！、\s])词([，。？！、\s]|$)`，**禁止** 删词中间的匹配
- 句尾助词（呀/呢/吧/啦/哦/啊）默认 **不删**：`好啊 → 好` 会改变语气

## G6 焦点恢复

- `SetForegroundWindow` 受系统限制：标准解法是先模拟 ALT 键按下/释放再调用，直接调用会随机失败

## G7 提权窗口

- 目标窗口 elevated 而本进程非提权：`SendInput` 被 UIPI **静默吞掉**
- MUST 注入前检测并在 UI 提示，禁止静默失败

## G8 首包冷启动

- onnxruntime 首次推理 + llama.cpp 首次 decode 各有 500ms+ 开销
- MUST 启动 warmup，否则首次输入必超 1.5s

## G9 量化后标点丢失

- int8 量化若 tokens / 前端文件版本不配套，会出现无标点
- 导出后 MUST 跑 smoke test 对比，RTF < 0.15（8 核 CPU）

## G10 幻觉

- 静音/噪声输入下 SenseVoice 可能输出"谢谢观看"类幻觉
- MUST 在 postprocess 黑名单整句过滤，且空结果不注入

## G11 语气词与前后文粘连

实测：TTS 说"呃，今天，我们嗯开个会"，SenseVoice 输出 `饿，今天我们嗯开个会。`

- 句首"饿"（=呃）被误识别，且后接逗号 → 可删，但删的是"饿"这个错字，不是"呃"
- 句中"我们嗯开个会"里的"嗯"**两侧都是汉字**，边界规则判定为词内，不删

结论：
1. 边界规则只能处理"带标点/空格"的语气词，粘连的删不掉——这是刻意取舍（删了会误伤 `呃逆`）
2. 粘连清理只能交给模型纠错（G32，默认关）；守卫会拒绝删词，所以不保证修掉
3. 测试用例 MUST 同时覆盖"能删"与"不能删"，禁止为了删粘连而放宽正则（会误伤词内字）

## G12 ITN 默认关闭：数字改写不算错，但它会拖垮混排英文

初版开了 ITN（`use_itn=True`），实测两类影响：

| 影响 | 例句 | 性质 |
|---|---|---|
| 数字改写成阿拉伯数字 | `一百五十毫秒` → `150毫秒`、`三点` → `3点` | 表面形式，正文里无法还原 |
| **混排英文退化** | `Xifeng 的 README` → `voiceice imME的readmin` | 真实错误，CER 4.5% → 27.3% |

同一批 5 句真人录音：`zh + ITN` 平均 CER 15.5%，`zh + 关 ITN` 3.3%。

对策：

- `asr.DEFAULT_USE_ITN = False`，ASR 只负责给原文，数字归一化交给 `postprocess` 的规则（M1）
- MUST NOT 在文本清理里把数字改回中文（等于自己实现一遍 ITN，且会和 ASR 输出打架）
- 关 ITN **不增加推理耗时**（512 vs 528ms，同 8 线程），所以这是纯收益的取舍
- `quality_check.py` 的 CER 口径因此不再需要为 ITN 留豁免；历史数据里的 ITN 偏差见 `04-reference/metrics.md`

## G13 提权检测读不到令牌

实测：前台是 Edge（`msedge.exe`），`OpenProcess(PROCESS_QUERY_INFORMATION)` 被拒，
`OpenProcessToken` 也失败 —— 但该进程并非提权，而是受保护（PPL）。

后果：
1. `elevated` 必须是三态：`True / False / None(未知)`。把 `None` 当 `False` 会漏掉真正的提权窗口；
   把 `None` 当 `True` 会让浏览器输入框直接不可用（主要场景）。
2. 当前策略：`True` 且本进程非提权 → 显式 `ElevatedTarget`；`None` → 按普通窗口尝试，
   失败靠 `SendInput` 返回值暴露。
3. **已知残留风险**：真正提权且受保护的窗口（部分管理工具）可能被静默吞键。
   M1 用"注入后校验/剪贴板兜底 + 用户可见反馈"收口，不能只靠令牌检测。

## G14 自动化注入测试需要交互桌面（且必须 Medium）

本机实测：从 agent 的 shell（Low，G21）里创建 Tk 窗口后，`SetForegroundWindow`、
`AttachThreadInput`、`SwitchToThisWindow`、`SetWindowPos(TOPMOST)` **全部无效**，
前台窗口始终不变（Edge 一直是前台）。

2026-10-29 复测：换成 `%TEMP%\vi-venv\Scripts\python.exe`（Medium）后，
ALT 技巧（G6）抢前台**成功**（`_restore_focus → True`），跨进程 WinForms 目标也能注入。
所以：

- 真实注入/剪贴板兜底的自动化验证 MUST 用目录外的 Medium 解释器
- Low 进程既抢不到前台，`SendInput` 也会被 UIPI 丢掉（G18/G21），只能验证纯逻辑
- 仅"窗口存在"不够，MUST 校验 `snapshot_focus().hwnd` 就是目标窗口再注入，
  否则会把测试文本打进当时真正的前台窗口（实测打到过 Chrome）

## G15 在 Tk 主线程里注入会"丢字"（测试自伤）

实测（2026-10-28，用户终端，chunk=32）：`inject_smoke.py` 第一版在 Tk 主线程里直接调 `inject()`，
长文本 184 字只读回 110 字、emoji 用例 14 字只读回 7 字——看起来像注入丢字，其实是测试自身的问题。

原因：`SendInput` 把 `WM_CHAR` 投递给目标窗口**所在线程**的消息队列。本进程主线程正卡在
`time.sleep`（块间 5ms）里，无法处理自己的队列，堆积到上限就丢。

对策（`inject_smoke.py` 已按此实现）：
1. worker 线程**只负责抢焦点 + 发键**，绝不碰 Tk 控件（Tkinter 非线程安全）
2. 主线程用 `root.after` 轮询读回，消息泵全程运转
3. 判定 MUST 轮询到"内容连续 200ms 不变"再比对，不能固定 sleep 一次就读
4. 正式程序里注入由独立线程发、目标是别的进程，不受此影响

> 状态（2026-10-28，用户交互终端实测）：
> - `chunk=8` → **5/5 通过**
> - `chunk=32`（默认）→ **5/5 通过**
> 结论：之前的丢字纯粹是"读回太早"，分块大小不是变量，`CHUNK_UNITS` 保持 32。
> 注入耗时（同进程 Tk 目标）：5 字 32ms / 17 字 159ms / 14 字 135ms / 7 字 185ms / 184 字 255ms。

## G16 uv 的 VIRTUAL_ENV 警告

现象：`warning: VIRTUAL_ENV=D:/project/workspace/.venv does not match the project environment path .venv`

原因：shell 里残留了指向别处的 `VIRTUAL_ENV`。uv 会自动忽略它并用自己的 `.venv`，
**警告可以不管**；想彻底安静就 `unset VIRTUAL_ENV`（PowerShell：`Remove-Item Env:VIRTUAL_ENV`）。

## G17 Ctrl 作 PTT 键的误触

改成 Ctrl 长按后，每次 `Ctrl+C` / `Ctrl+V` 都是一次"按下-松开"。
若不做保护，复制粘贴会触发录音并把环境噪声注入光标。

对策：`min_hold_ms = 250`（`spike/xifeng/hotkey.py`）。按住不足 250ms 直接丢弃，
不进 ASR、不产生 Trace。实测 Ctrl+C 的按住时长约 60–120ms，与门槛有足够间距。

两个待观察项：

1. **粘滞键**开启时 Ctrl 会保持"按下"状态 → 会一直录到 `max_seconds`（60s）才截断。
   `WH_KEYBOARD_LL` 能拿到真实 KeyDown/KeyUp，但粘滞键本身不发 KeyUp，M1 需要检测并提示。
2. 部分全屏应用里按住 Ctrl 时滚轮缩放 —— 可接受，不处理。

## G18 `SendInput` 自测按键状态不可靠

想验证"能不能读到键"，第一反应是用 `SendInput` 造一个按下再看 `GetAsyncKeyState`：

```python
user32.SendInput(1, keydown_F13, ...)
print(user32.GetAsyncKeyState(0x7C) & 0x8000)   # → 0
```

实测（VK 注入与扫描码注入都试过）**结果恒为 0**。但这台机器的 shell 是 Low 完整性（G21），
`SendInput` 虽然返回成功计数（2），事件实际被 UIPI 丢掉了 —— 所以这个实验**证明不了任何事**。

结论：不要在自动化环境里用 `SendInput` 自测键读取。要么用 `key_diag.py` 让真人按，
要么在 Medium 完整性的正常终端里验证。

## G19 钩子在该环境下收不到事件

实测（2026-10-28，agent 的 shell）：`SetWindowsHookExW(WH_KEYBOARD_LL)` **安装成功**
（返回非 NULL），但回调一个事件都收不到。

根因见 G21（Low 完整性），不是钩子写法问题。用户终端里 `--diagnose-keys` 也是 0 事件，
需用 `key_diag.py` 逐项排查（它会打印完整性、会话、前台归属，并区分物理/注入事件）。

踩到的两个实现坑（与完整性无关，真实存在）：

1. `hMod` 传 `GetModuleHandleW(NULL)` → `ERROR_MOD_NOT_FOUND(126)`。
   两个原因叠加：ctypes 未声明 `restype` 导致 64 位句柄被截成 32 位；
   LL 钩子本就该传 `NULL`（回调在安装进程内执行，不需要 DLL）。
2. 装钩子前 MUST 用 `PeekMessage` 强制创建本线程消息队列，否则可能收不到事件。

影响：

- 自动化环境无法验证 PTT，只能由用户在真实终端按一次
- 诊断 MUST 打印心跳事件计数，否则无法区分"没按键"与"收不到事件"
- 心跳 MUST 同时打印注入事件数，否则会漏掉 G20（重映射键盘）

## G20 键盘重映射软件产生的按键也是 `LLKHF_INJECTED`

最初为了"注入的文字不要反过来触发 PTT"，钩子无条件忽略带 `LLKHF_INJECTED` 的事件。
这条规则在装了键盘重映射的机器上会让 PTT **完全失灵**：PowerToys Keyboard Manager、
厂商驱动（Lenovo / Huawei 等）、AutoHotkey 这类工具替用户发出的按键全部是 injected。

对策：**只忽略自己发的事件**。

- `inject.py` 所有按键事件都带 `dwExtraInfo = EXTRA_INFO_MAGIC`（`0x56494D45`，即 `'VIME'`）
- 钩子读到 `KBDLLHOOKSTRUCT.dwExtraInfo == MAGIC` 才忽略，其它 injected 事件照收
- 两处常量 MUST 保持一致（`inject.EXTRA_INFO_MAGIC`，`hotkey.py` 直接 import 它，别复制字面量）

代价：别的宏工具如果注入 Ctrl 按下/松开，会触发一次 PTT。可接受 —— 那本来就是"用户想按键"。

## G21 Low 完整性下全局钩子收不到硬件按键

2026-10-28 用 `spike/key_diag.py` 测出真正原因：**进程运行在 Low 完整性级别（`0x1000`）**，
不是"这台机器读不到按键"。

关键证据（同一次运行同时出现）：

```
完整性=Low(0x1000)
钩子：事件 0 个
Tk 窗口：收到 43 个键 → ['Control_R', ..., 'a', ...]
本进程窗口是否拿到前台：True
```

即：**按键进了进程，但钩子没被调用**。Windows 对低级键盘钩子做完整性检查
（硬件输入的来源视为更高完整性），对普通窗口消息不做。

| 现象 | 在 Low IL 下为什么必然发生 |
|---|---|
| `WH_KEYBOARD_LL` 装得上但 0 事件 | 钩子回调被完整性检查跳过；装钩子本身不检查 |
| `GetAsyncKeyState` 对所有 VK 恒 0 | 异步键状态对 Low IL 不可见 |
| Tk 窗口能收到 `Control_R` / `a` | 普通 `WM_KEYDOWN` 消息不受 UIPI 限制 |
| agent shell 与用户 MINGW64 都是 Low | 终端是 agent 拉起的子进程，继承受限令牌 |
| `SendInput` 返回成功计数但目标窗口收不到 | 事件被 UIPI 静默丢弃（G18） |

**不是输入法的锅**：搜狗/微软拼音只处理按键之后的文本，不会阻止钩子被调用
（钩子链上每个钩子都会被调，谁也拦不住谁）；而且 Tk 收到了 43 个 `Control_R`，
说明 Ctrl 完整到达了进程。关闭搜狗后现象不变，已排除。

**定位方法**：拿同一个 `python.exe` 分别在两个目录启动，看进程完整性 ——
`whoami /groups` 看的是外层 shell，不是子进程，会骗人。

| 启动的 exe 位置 | 进程完整性 |
|---|---|
| `D:\env\python\python.exe` | Medium |
| 同一个 exe 复制到 `%TEMP%` | Medium |
| 同一个 exe 复制到本仓库目录 | **Low** |
| `.venv\Scripts\python.exe`（在仓库内） | **Low** |

根因是**目录标签**：

```
> icacls D:\project\workspace
        Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)      ← 显式低完整性标签
> icacls D:\project\workspace\voice-ime
        Mandatory Label\Low Mandatory Level:(I)(OI)(CI)(NW)   ← (I) 继承而来
```

即 `D:\project\workspace` 这个工作区根目录带 Low 完整性标签，
从里面启动的任何可执行文件都继承受限令牌。改标签需要写 SACL 的权限，
当前环境里 `icacls ... /setintegritylevel Medium` 被拒绝（`拒绝访问`）。

**对策**（进程完整性只由 exe 决定，与 CWD 无关）：

- 用目录外的解释器跑：
  `set PYTHONPATH=.venv\Lib\site-packages && py -3 key_diag.py`
  （诊断不需要 sherpa；跑 `--ptt` 时加 `--backend mock`）
- 或把 `spike\` 复制到未打标的目录（如 `%TEMP%\vi-run`）后重跑
- `run_spike.py --ptt` 启动时直接打印完整性级别，Low 时立刻警告，不再让用户白等

本环境里唯一能验证的时序语义是 `key_diag.py` 第 5 步：Tk 窗口里按住 Ctrl，
量真实按住时长与 `min_hold_ms` 门槛 —— 窗口消息可用，钩子不可用。

**已闭环（2026-10-29 22:07）**：把 exe 放在 `%LOCALAPPDATA%\Xifeng-build\dist\Xifeng`
（Medium）后，**物理 Ctrl** 已实测被钩子收到 —— SQLite 里出现 `mode=ptt`、
`speech_ms=16601`、`outcome=Injected` 的记录，说明 Low 是唯一障碍，不是 UIPI/安全软件。
判据仍然是 exe 启动第一行的 `完整性=Medium(0x2000)`。

## G22 `language=""`（自动检测）会把普通话判成粤语

实测（2026-10-29，同一条真人录音）：

| 配置 | 句1 输出 | CER |
|---|---|---|
| `language=""`（auto） | `听日下午三点开会落班返来` | 57.1% |
| `language="zh"` | `今天下午三点开会把方案发给我` | 0.0% |

5 句平均：auto 17.4% → zh **3.3%**。ASR 的自动语言识别在短句 + 口音下极不稳定，
而且错了不会报错，只是整句变成另一种语言。

对策：

- `asr.DEFAULT_LANGUAGE = "zh"`，语言 MUST 显式传入，禁止依赖 auto
- 换语言（英文/日文）要走配置项 + 用户显式选择，不能靠模型猜
- 固定 `zh` 的副作用：混排英文词会被按中文音素拆（`Xifeng` → `voice i e`），
  属模型能力边界，靠可选的模型纠错补（G32），不属于配置问题
- 固定 `zh` / 关 ITN 都**不增加推理耗时**（512 vs 528ms），可以放心写死

## G23 开流静默期与松手尾音：按住 1.5s 只收到 0.1–1.3s

2026-10-29 实测：`--ptt` 按住 1.5s，`[mic]` 打印的长度在 0.10s / 1.33s / 1.36s 之间乱跳，
ASR 因此拿到半句话（或空音频）。两处丢音，原因不同：

**A. `start()` 到第一个回调之间有静默期**（同一台机器，1.5s 收帧统计）：

| 配置 | 首包延迟 | 实收 / 1.50s |
|---|---|---|
| `blocksize=1024` 默认延迟 | **376ms** | 1.47s |
| `blocksize=0` 默认延迟 | 95ms | 1.46s |
| `blocksize=1024` + `latency="low"` | 172ms | 1.47s |
| `blocksize=0` + `latency="low"` | 175ms | 1.48s |

首包延迟不是回调"变慢"，而是**流真正开始供数之前的时间**，这段时间的音频不存在。
所以"按下热键才 `start()`"必然吃掉句首 —— 而"不按键就关麦"又要求必须按下才开流，
两者只能取舍（默认按需开麦，见下方对策与代价）。

> **2026-11-14 复测（D16 后的壳侧 cpal）**：Tauri 壳的采集换成 cpal 后，
> `play→首回调` 实测 **17–24ms**，上表是旧 sounddevice/PortAudio 栈的数字。
> 因此「按下即开流 + 立刻弹浮窗」本身就等于零等待，不必再用 `wait_live` 等流；
> B 段的 `drain_ms` 仍然必需。
>
> **2026-11-15 修正**：`play→首回调` 只是**建流之后**的那一段。真正的大头在
> `build_input_stream`（WASAPI `IAudioClient::Initialize`）：热态 **99–120ms**、
> 冷启动 **671ms**，`play()` 本身 0ms。所以"按下即开流"并不等于零等待，
> 浮窗实际要等 ~155ms（热态）/ ~690ms（冷启动）——详见 G41。

**B. 松手立刻 `stop()` 丢掉宿主缓冲里的尾音**（按住 1.50s，同一份代码只改参数）：

| `latency` | drain | 实收 | 偏差 |
|---|---|---|---|
| 默认 | 0 | 1.33s | −0.17s |
| 默认 | 150ms | 1.46s | −0.04s |
| `low` | 0 | 1.33s | −0.17s |
| `low` | 100ms | **1.51s** | +0.01s |

回调块大小由宿主决定，本机 16k 流约 **416 帧/块（≈26ms）**，`stop()` 之前还在宿主
缓冲区里的那几块直接消失 —— 表现为"最后一个字没识别出来"。

**对策（默认按需开麦，四条同时成立）**：

1. `_open_stream()` 用 `blocksize=0` + `latency="low"`（首包 376ms → 95–175ms）
2. **默认按下才开麦**：`record_ptt` 先 `wait_for_press()`，确认按下后才 `LiveMic.start()`；
   松开、drain 完立刻 `stop()/close()`。不录音时麦克风完全关闭，Windows 麦克风指示不常亮
   （这是用户明确要求，也是对"不录音不占设备"的唯一正确实现）
3. 开流后到 `begin()` 之间已经到达的头几块仍并入本次录音（`pre_ms`），不额外丢句首
4. 松手后再收 `drain_ms=120` 才 `stop()`（取消时不收，要立刻收工）

代价：每次按下都有 100–400ms 的开流静默期，这段音频**不存在**，抢话必丢句首。
产品上的做法是让提示音/浮窗先出现，用户看到"录音中"再开口。

想彻底消掉这段静默期，显式加 `--resident-mic`：启动即 `hotkey.start_mic()` 开流，
回调空闲时只维护最近 `pre_ms=300` 的前置缓冲，`record_ptt` 只在缓冲区上打切片
（`LiveMic.begin()/end()`）。代价是常驻期间麦克风一直打开（Windows 指示常亮），
仅在明确接受该代价时使用。

e2e 验证（Medium 进程 + 自注入 Ctrl 驱动真实 `--ptt`，常驻流模式的历史数据）：

```
按住 1.5s → [mic] 2.15s → outcome=Injected
按住 1.2s → [mic] 1.82s → outcome=Injected
```

长度比按住时间长是**正常的**：`pcm` = `pre_ms` 前置缓冲 + 按住时长 + `drain_ms` +
回调抖动；`PttResult.seconds` 只算按住时长（不含 pre/drain），误触判定用它。

音频只在内存缓冲里，不落盘、不出本机（与 `AGENTS.md` 的隐私约束一致）。

## G24 PyInstaller 打包：产物落在 Low 目录 → 钩子失效、启动变慢

2026-10-29 用 PyInstaller 6.22.3 打出 onedir 交付物，踩到五个坑，前两个会直接让功能不可用。

**A. 在仓库里构建 = 打出 Low 完整性 exe（功能直接废）**

第一次 `uv run python packaging/build.py`，`tempfile.gettempdir()` 返回
`C:\Users\...\Temp`，但实际产物落在 `spike\xifeng-dist`。原因：

```
uv run → .venv\Scripts\python.exe（在 Low 目录里创建 → 进程 Low）
Low 进程写 Medium 目录 → no-write-up 拒绝
tempfile._get_default_tempdir() 探测失败 → 静默退回当前目录
```

于是 dist 在仓库内（Low 标签）→ exe 也是 Low → 全局钩子收不到硬件按键（同 G21）。
**判据**：`Xifeng.exe --ptt` 第一行必须是 `完整性=Medium(0x2000)`。

对策：构建必须用**仓库外的解释器**，输出到 `%LOCALAPPDATA%`：

```bat
uv venv %TEMP%\vi-venv --python 3.11
set PYTHONPATH=<spike>;<spike>\.venv\Lib\site-packages
%TEMP%\vi-venv\Scripts\python.exe packaging\build.py
```

注意：即使终端是 Medium，`uv run` 用的仍是仓库内的 `.venv\Scripts\python.exe`，
它本身是 Low —— "终端是 Medium" 不等于"子进程是 Medium"（G21 同一根因）。

**B. `__file__.parents[2]` 在冻结后指向 `_internal`**

源码形态模型在 `<仓库根>\models`；打包后 `__file__` 在 `_MEIPASS/_internal/...`，
`parents[2]` 与源码假设不一致，模型找不到、`backend=auto` 静默退回 mock。
对策：`xifeng/paths.py` 统一解析 —— 冻结时 `<exe 同级>/models/asr/...`，
源码时 `<仓库根>/models/asr/...`，`IME_MODELS_DIR` 始终最高优先。

**C. sherpa-onnx 的 DLL 必须显式收集**

`_sherpa_onnx.cp311-win_amd64.pyd` 依赖同目录的 `onnxruntime.dll`（17MB）、
`sherpa-onnx-c-api.dll`、`sherpa-onnx-cxx-api.dll`；不收集会在 import 时报
`DLL load failed`。`sounddevice` 另需 `_sounddevice_data` 里的 portaudio DLL。
对策：`--collect-all sherpa_onnx --collect-all sounddevice --collect-all _sounddevice_data`。

**D. onefile 每次启动解压 ~25MB**

onefile 把 `onnxruntime.dll` + `.pyd` + openblas 打进单文件，每次启动解压到临时目录，
冷启动多 1–2s（spec 要求 <5s，但没必要浪费）。对策：onedir。

**E. 模型 228MB 不能塞进 exe**

onefile 会把模型也解压一遍；onedir 里它只会让目录变大但启动不变慢。仍推荐外置：
程序 145.6MB 可单独升级，模型目录独立替换。

实测（本机）：

| 项 | 数字 |
|---|---|
| 构建（`--clean`） | 34.4s |
| 程序（不含模型） | 145.7MB / 1228 文件；exe 5.5MB |
| 程序 + 模型 | 374.1MB / 1230 文件 |
| `--text` 冷启动 | 1.98s |
| `--wav` 4.61s 音频 | 3.67s 全程，ASR 352ms |
| `--ptt` 自注入 1.5s | 收 1.94s，1/1 注入，Medium |

> 加入 tkinter（GUI）与 pystray/Pillow（托盘）后，程序体积从 122.8MB 涨到 145.7MB、
> 文件数从 57 涨到 1228 —— 主要是 Tcl/Tk 的数据文件。**不要**为了体积去掉 GUI；
> 它们都在同一个进程包里，托盘和窗口共用一份。

## G25 SQLite 在 Low 进程里建不了库：`%LOCALAPPDATA%` 是 no-write-up

2026-10-29 接数据层时踩到。`Store()` 默认路径想放 `%LOCALAPPDATA%\Xifeng\xifeng.db`，
但在本工作区跑（`.venv\Scripts\python.exe` 是 Low 进程）直接
`sqlite3.OperationalError: unable to open database file` —— 看起来像"路径不存在"，
其实是**目录建不出来**：Low 进程对 Medium 目录（`%LOCALAPPDATA%`）只有读权限。

`mkdir(parents=True, exist_ok=True)` 抛的是 `PermissionError`，但如果只捕获
`sqlite3.OperationalError` 就会误判成"库文件损坏"。更隐蔽的是 `%TEMP%`：
`tempfile.gettempdir()` 探测失败会**静默退回当前目录**（同 G24），
于是库悄悄落到仓库里。

判据：`paths.data_dir()` 会先做一次"建目录 + 写探针文件"，失败才回退。打印
`[db] db=...\xifeng.db` 时确认路径：

- 源码 + Low 进程 → `<spike>\.data\xifeng.db`（正常，预期行为）
- 源码/冻结 + Medium → `%LOCALAPPDATA%\Xifeng\xifeng.db`
- 冻结 + `%LOCALAPPDATA%` 不可写 → `<exe 同级>\data\xifeng.db`

不要为了"便携"把库强制放 exe 同级：PyInstaller `--noconfirm` 重建 dist 会**清空整个目录**，
上一版的历史会一起没（本机实测：重建后 `data\xifeng.db` 消失）。
用户数据放 `%LOCALAPPDATA%` 才能跨版本保留。

另外两个相关坑：

- **WAL 需要写目录权限**：`PRAGMA journal_mode=WAL` 会在库旁边建 `-wal`/`-shm`；
  只读介质（或只给文件写权限的共享目录）上会失败。对策：`Store` 里 WAL 失败不致命，
  但要在日志里看得出来。
- **不要用 `:memory:` 做主库**：进程退出即丢，且无法与将来的 GUI 共享。

对策汇总：

```python
# 先探可写，再决定落点（paths.data_dir）
try:
    d.mkdir(parents=True, exist_ok=True)
    (d / ".write-probe").write_text("ok"); (d / ".write-probe").unlink()
except OSError:
    d = app_dir() / ".data"      # 保底：程序目录内，一定可写
```

要强制指定位置：`IME_DATA_DIR=...`（目录）或 `IME_DB=...`（文件）。

## G26 托盘/GUI 启动就崩：`print` 的编码与缺失的 stdout

接托盘与 GUI 时，`--gui` 在本机直接弹「打不开主窗口：'gbk' codec can't encode character
'\u26a0' in position 5」。原因不是 tkinter，也不是数据库，而是**日志流**：

- `Store` 打开 WAL 失败时会 `print("[db] ⚠ ...")`
- 从托盘/自动化启动时 stdout 被重定向到管道，Windows 默认按 `cp936/gbk` 打开
- `⚠` 不在 gbk 里 → `UnicodeEncodeError` → 冒泡到 `App.__init__` → 窗口打不开

更隐蔽的是**冻结的 windowed exe**：`sys.stdout is None`，任何一句 `print` 都是
`AttributeError: 'NoneType' object has no attribute 'write'`。这类崩溃看起来像 GUI/数据库问题，
其实是"程序没有地方写日志"。

对策（`xifeng/applog.py`）：

```python
# GUI：有流就只把编码改成 UTF-8（errors=replace），没有流就落文件
applog.setup()
# 托盘：控制台要隐藏，强制落文件
applog.setup(force_file=True)   # %LOCALAPPDATA%\Xifeng\xifeng.log
```

- MUST 在做任何可能 `print` 的事情（开数据库、加载模型）**之前**调用
- 日志轮转：超过 2MB 改名为 `xifeng.log.1`
- 只记长度/结果/trace，绝不记识别文本全文

验证：`--tray` 启动后 `xifeng.log` 第一行应是 `[tray] 已启动…`，且进程
`MainWindowHandle=0`（控制台已隐藏）。

## G27 两个托盘实例各装一个钩子：一次按键录两份音

`--tray` 是常驻进程，用户很容易"以为没启动"又双击一次（开机自启 + 手动启动也会重叠）。
两个实例都装 `WH_KEYBOARD_LL`、都开麦克风，结果同一句话被识别两次、注入两次。

对策：命名 Mutex（`Local\\Xifeng-Tray`）。第二个实例在加载模型**之前**就退出，
并往日志写 `[tray] 已有实例在运行`。进程崩溃时内核自动释放，不会留下假锁。

Mutex 只挡托盘（resident）形态；`--ptt --times 1`、`--gui`、CLI 子命令仍可并行，
因为它们是短生命周期或只读的。

## G28 剪贴板兜底：五个会让它"看起来成功了"的坑

`--inject clipboard` / `auto` 走 `inject.py` 的剪贴板路径，实测踩到：

**A. WinForms 的 Edit 类名带前缀，且不认 `EM_GETTEXTLENGTHEX`**

焦点控件类名是 `WindowsForms10.EDIT.app.0.xxx`，不是裸 `Edit`；用等值匹配会永远探测不到，
`auto` 就永远不兜底。另外 `EM_GETTEXTLENGTHEX` 在它上面恒返回 0
（`WM_GETTEXTLENGTH` / `GetWindowTextLengthW` 正常）。对策：

- `is_edit_class()` 同时支持裸类名与 `WindowsForms10.EDIT` / `WindowsForms10.RichEdit` 前缀
- 统一用 `WM_GETTEXTLENGTH` + `WM_GETTEXT`（都带 `SendMessageTimeoutW`，50ms 超时，防卡死）

**B. `SendInput` 是异步的：发完立刻读会读到"只打了两个字"**

目标线程还没处理完队列，探测到的 `length=2` 并不代表注入失败。若据此兜底，会把整句再粘一遍。
对策：`_wait_for_change()` 连续观察 120ms，只有**持续**没变化才判定失败。成功路径第一次探测
就变了，不付这个延时（实测 auto 28–39ms）。

**C. 恢复太快，粘出来的是原剪贴板内容**

`Ctrl+V` 发出 ≠ 目标已读走剪贴板。对策：能探测时轮询"文本已变"，之后仍等满
`clipboard_restore_ms`（默认 500ms，G3），再恢复。

**D. `Ctrl+V` 自己也是注入事件**

不签名就会触发 PTT（G17/G20）。`_send_paste()` 的四个按键事件 MUST 都带 `EXTRA_INFO_MAGIC`。

**E. 只能快照 HGLOBAL 型格式**

`EnumClipboardFormats` 里 `CF_BITMAP` / `CF_ENHMETAFILE` 是句柄型，`GlobalSize` 读不到，
复制不了。遇到时提示"恢复可能丢失"，但仍把能存的都存下来；剪贴板被占用时 `OpenClipboard`
重试 5×20ms，仍失败就显式 `Failed`，不静默。

其它约束：

- 不得为了兜底把 `auto` 用在读不到文本的窗口（控制台/游戏）—— 会打两遍
- 提权目标对两种策略都必须 `ELEVATED_TARGET`（G7）
- 单测 MUST 注入假剪贴板（`tests/test_inject.py::FakeClipboard`），禁止碰用户真实剪贴板

## G29 tkinter 自绘控件的三个隐形坑

**A. `pack(in_=row)` 的控件会被 row 的底色盖住**

`Page.row()` 里控件 master 是 group 的 `body`，pack 用 `in_=row` 落进行内 —— 但
z 序按创建顺序排，后创建的 row 画在控件上面，控件"布局正确却看不见"。
对策：pack 之后 `tk.Misc.tkraise(control, row)`。
**不能用 `control.lift()` / `control.tkraise()`**：Canvas 子类（Button/Select/
Switch/Segmented/Slider 全是 Canvas）把这两个名字重载成了"提升画布元素"，
传 Widget 进去报 `invalid boolean operator in tag search expression`。

**B. `def f(bg=CARD)` 把颜色定格在 import 时刻**

默认参数在函数定义时求值。切主题靠改模块级令牌，定格的默认值不跟着变 ——
浅色主题下会画出深色残块。对策（`ui.py`）：所有颜色默认参数 MUST 写成
`None` 并在函数体内解析（`bg = bg or CARD`）。回归用例：`tests/test_ui.py`。

**C. 占位符 MUST NOT 写进绑定的 StringVar**

`ui.Input` 曾把占位文本 `var.set(placeholder)` 来显示 —— 外部代码直接读
var 时就把占位文本当成了用户输入（历史页首屏搜索"搜索文本…"、空输入框被当成有效值）。
对策：占位符用覆盖 Label 显示，var 永远只装真实输入。

## G31 Web GUI（Edge 应用模式）的调试与截图

- **宿主**：`--web` = 标准库 HTTP（仅 127.0.0.1:8642，单线程）+ `chrome --app=URL`
  （用户指定 Chrome；Edge 已弃用——自动化窗口与用户浏览器互相干扰）。
  pywebview 方案废弃：pythonnet 在本机报 `Failed to resolve Python.Runtime.Loader.Initialize`。
- **Chrome/Edge 从 Low 进程启动会静默不建窗口**（Chromium 拒绝低完整性）。开发时用 Medium
  解释器代启动：`python -c "import subprocess; subprocess.Popen([EDGE, ...])"`。
- **UIPI 拦一切跨完整性窗口操作**：Low 进程对 Medium 的 Edge 窗口
  `GetWindowTextW` 恒为空（别按标题找窗口，按进程名+最大可见窗口找）、
  `PrintWindow` 失败（黑帧/直接返回 0）。
- **可靠截图走 CDP**：`--remote-debugging-port=9222` **必须配独立
  `--user-data-dir`**（新版浏览器禁止默认 profile 开调试端口）。
  工具：`spike/cdp_shot.py` / `cdp_ask.py`（自写最小 WS 客户端，忽略 ping/pong 帧）。
- **`--window-size` 单位是 CSS 像素**：dpr=1.5 时物理窗口是 1770px。
  PrintWindow/GetWindowRect 都是物理像素 —— 尺寸对不上时截图只截到局部，
  看起来像"布局溢出"，其实是截图 artifacts。
- **改前端后必须 reload**：hash 跳转不重新加载 bundle；CDP `Page.reload` 后
  旧的 `Page.addScriptToEvaluateOnNewDocument` 陷阱仍然有效。
- **前后端版本错位会黑屏**：webgui 服务器是长驻进程，改完 payload 字段/前端
  MUST 重启服务器 + reload 页面，二者只更新一个就会出现
  `Cannot read properties of undefined` → React 整树卸载 → 全黑。

## G30 Low 进程检视 GUI / 读数据

- 给 GUI 做视觉回归用 `spike/ui_shots.py`（PrintWindow 离屏抓帧，不抢前台、
  不需要 Medium）。临时产物一律进 `<仓库根>/temp/`。
- 检视用**库副本**：直接 `sqlite3.connect()` 打开 `%LOCALAPPDATA%` 里的 WAL 库
  会 `unable to open database file`（只读也要建 -shm，Low 进程建不了）。
  对策：`shutil.copyfile` 连同 `-wal`/`-shm` 复制到 temp 再打开副本。
- 截 Toplevel 弹窗前 MUST `update()` 两遍并等 ≥150ms，否则 PrintWindow 抓到全黑帧。

## G31 Web GUI：Edge 应用窗口 + 本地服务 + 心跳

交付界面从 tkinter 换成 `spike/frontend`（vite + React + shadcn/ui）+ `webgui.py`，
踩到的四个点：

**A. 不要用 WebView 壳（pywebview/Tauri）**

pywebview 依赖 pythonnet，本机 .NET 运行时加载失败；Tauri 要 ~6GB Rust 工具链（D6 已推迟）。
用系统自带 Edge 的 `--app=http://127.0.0.1:8642` 开无地址栏窗口，宿主保持纯 Python。
无 Edge 时只打印地址，不静默失败。

**B. 服务只绑定 127.0.0.1，且必须单线程**

`ThreadingHTTPServer` 会让请求落在线程池里，而 `Store` 是在主线程创建的
（`sqlite3.check_same_thread`），一并发就报错。GUI 没有并发压力，`HTTPServer` 串行足够。
绑定地址 MUST 是 `127.0.0.1`：音频/文本/历史都不出本机。

**C. 每次点托盘菜单都起新进程 → 端口冲突与僵尸进程**

- 起服务前先探端口：已占用且 `/api/bootstrap` 返回本程序的字段，就**只开窗口**复用；
  不是本程序则报错退出（`return 1`），绝不抢端口
- 前端每 5s `POST /api/ping`；服务侧看门狗 30s 没心跳就 `shutdown()`。
  否则关窗后 python 进程一直挂着，下次点菜单又起一个，越积越多

**D. 前端构建产物位置**

源码跑用 `spike/frontend/dist`；冻结包 MUST 复制到 `<exe 同级>/frontend`
（`packaging/build.py` 已处理）。改了 `src/` 不重新 `vite build`，界面看到的还是旧 dist ——
排查"样式没生效"先比 `dist/assets` 与 `src` 的修改时间。

验证：`spike/cdp_shot.py` 走 CDP 截五页（stats/models/config/history/about）。
Edge 必须用独立 `--user-data-dir` + `--remote-debugging-port=9222` + `--new-window` 启动，
且脚本要按 `http://127.0.0.1` 过滤页面 —— 首次启动的同步/欢迎页也是 `type=page`，
不筛就会截到 Edge 自己的弹窗。

收尾时 MUST NOT `Stop-Process -Name msedge`：会连用户自己开的窗口一起关掉。
只关自己以独立 profile 启动的 PID（或按 `--user-data-dir` 过滤），
也 MUST NOT 复用/接管用户已开的浏览器窗口。

## G32 模型纠错：会删词、会续写，必须默认关 + 守卫

纠错从"人写术语表"改成"本地 Qwen2.5-0.5B-Instruct Q4_K_M（491MB）+ llama.cpp"。
同一句 prompt 在 CPU 上实测（8 线程）：

| 输入 | 输出 | 结果 |
|---|---|---|
| `这个会议机要发给我` | `这个会议纪要发给我。` | ✅ 同音字 |
| `请把 red me 文件发给张三` | `请把 README 文件发给张三` | ✅ 专有名词 |
| `帮我把 voice i e 的 read me 提交` | `把Xifeng的README提交` | ❌ **"帮我"被吞** |
| `今天的会推迟到下午三点` | `今天的会推迟到下午三点二十。` | ❌ 句尾续写 |
| `这个会议机要发给我` | 原样返回 / 补句号 | 不稳定（0.5B 能力边界） |

**结论：不能默认开，也不能"模型说啥就注入啥"。**

- **默认关**：`Settings.model_correct = False`；配置页只有一个开关，没有术语表
- **模型位置**：`%LOCALAPPDATA%\Xifeng\models\llm\qwen2.5-0.5b-instruct-q4_k_m.gguf`
  （与 SQLite 同目录，**不进 dist/exe**；`IME_LLM_MODEL` 可覆盖）。缺失时开关禁用
- **守卫**（`correction.check`）— 任一命中就回退原文：
  - 句首删字（`drop-start`，只允许语气词）
  - 连续删除 > 3 字（`drop`）；总删除 > 1/4（`drop`）
  - 句首/句尾新增内容（`edge-add`，模型在续写）
  - 只改标点（`punct-only`，拒绝强行补句号）
  - 多行/`输出：`/```/标签 → 解释性输出（`explain`）
  - 字符相似度 < 0.80 或长度差 > 3/25%（`diff`/`length`）
- **延迟**：模型加载 ~1.1s，warmup ~1.7s，热身后 **0.35–1.5s/句**（短句 0.35s，长句/冷启动更慢）。
  所以开启时 MUST 在 `Pipeline.warmup()` 里热身；否则第一句必然 +4s，直接破 1.5s
- **降级**：模型缺失/`llama_cpp` 加载失败只警告一次，然后按清理后的原文注入，绝不逐句重试
- **隐私**：日志只记 `ms_correct` 与拒绝原因（枚举），不记文本

验证口径：`uv run python run_spike.py --text "..." --correct --no-inject` 看
`[correct] applied/保留原文`；`quality_check.py --correct` 量开启纠错后的 CER。


## G33 冻结包 `--correct` 崩在 `llama_backend_init()`：旧版 msvcp140 顶掉系统版

现象（只在 PyInstaller 产物里复现，源码形态完全正常）：

```
[correct] 模型加载失败：加载失败：OSError: exception: access violation reading 0x0000000000000000
  File "llama_cpp\llama.py", line 208, in __init__
    llama_cpp.llama_backend_init()
```

排查过程中**全部排除**（改完仍然崩，别再走一遍）：

| 怀疑对象 | 验证方式 | 结论 |
|---|---|---|
| `_internal` 根目录与 `_internal/llama_cpp/lib` 各有一份 `llama.dll`/`ggml*.dll` | 删掉根目录那份 / 只留 `lib` 那份 | 无关（哈希相同，删任一份照崩） |
| DLL 依赖解析顺序 | `LLAMA_CPP_LIB_PATH` 指向根目录 | 无关 |
| 预加载顺序（`ggml-base → ggml → ggml-cpu → llama`，`RTLD_GLOBAL`） | 运行时钩子 | 无关 |
| onnxruntime / OpenMP 冲突 | 源码里先 `import sherpa_onnx` 再 load | 无关（源码形态都正常） |
| PyInstaller 收集不全 | `llama_cpp/lib` 里 `.dll` 齐全、`llama_cpp.py` 能 import | 无关 |

真正原因是**运行库版本混用**：PyInstaller 从某个依赖里收进 `_internal\MSVCP140.dll` **14.31**，
而 Windows 优先加载 exe 同目录的副本，于是 14.31 顶掉了系统的 **14.42**；
`llama-cpp-python 0.3.36` 的 `llama.dll`/`ggml-cpu.dll` 是 **14.40** 工具链构建的，
混用旧 `msvcp` 就在第一次调 `llama_backend_init()` 直接段错误。

定位手法（最省时间的一步）：把 `_internal\MSVCP140.dll` 改名 → 立刻 `[correct] applied`。
再分别只回滚其中一个，确认是 `msvcp140` 而不是 `vcomp140`。

**修复**：`packaging/build.py` 的 `fix_msvc_runtime()` —— 打包末尾用 `System32` 里**更新**的
`msvcp140/vcruntime140/vcruntime140_1/concrt140` 覆盖产物里的旧副本（比较 `FILEVERSION`，
系统版更旧就保留原样）。

- **不要用"删掉这个 DLL"来修**：目标机可能没装 VC redist，删了就换成"找不到 DLL"
- 改这里 MUST 用版本号比较，不要写死"删 msvcp140"
- `IME_DEBUG=1` 会打开 llama.cpp 日志并在加载失败时附带 traceback（`correction._debug()`）
- 同类坑还会以别的形式出现：任何 14.3x 的 CRT 都可能和 14.4x 构建的 wheel 打架

## G34 录音浮窗的毛玻璃/圆角：Tk 不是 WPF，别指望 DWM 亚克力

浮窗（`xifeng/popup.py`）是 300×32 的细长条：左边 22px 应用图标，右边整条
镜像音柱，说话时随实时音量跳动。想要"毛玻璃 + 圆角"，踩了两条坑：

| 做法 | 结果 |
|---|---|
| `SetWindowCompositionAttribute` + `ACCENT_ENABLE_ACRYLICBLURBEHIND` | 调用成功（返回 1），**视觉零变化** —— 亚克力只对"没被画上去的像素"生效，Tk 画布是不透明矩形，盖上去就糊不起来 |
| `root.attributes("-alpha", 0.9)` | 整窗（含文字/音柱）一起变淡，背景并没有被模糊，看着像"调了透明度"而不是毛玻璃 |
| `ImageGrab.grab()` 整屏截图 | 能糊，但 60–85ms；`bbox=` 参数反而更慢（150ms+） |
| **BitBlt 直接抓一块**（当前实现） | 15–18ms，且坐标就是 Tk 逻辑坐标（进程 DPI 不感知，GDI 也是同一套虚拟化坐标），不用换算缩放 |

**当前实现**：`show()` 时先 `withdraw()` 再 BitBlt 抓浮窗位置那块屏幕 → 4× 超采样
+ 高斯模糊 + 主题色叠加 `_Glass.tint` → 缩回 300×32，圆角用 `-transparentcolor`
抠成透明（Tk 窗口只有"整像素透明"，所以边缘 alpha 要**阈值化**成 0/255，否则留一圈脏边）。

- 失败 MUST 静默退回半透明纯色面板（`theme.panel` + `-alpha`），浮窗坏了不能影响输入
- 窗口 MUST **只建一次**复用（`close()` 只 `withdraw()`）：Tk 初始化 ~130ms，
  每次按下都重建会让指示条慢半拍；复用后每次显示 30–50ms
- 截图前 MUST 先 `withdraw()`，否则底图里会拍到上一次的浮窗
- 音柱的音量来自 `LiveMic.last_chunk`，RMS 在 `tick()` 里算 —— 音频回调里仍然只拷贝

## G35 PyO3 扩展在本机构建：`maturin` 建临时目录被拒、`link.exe` 打不开 `%TEMP%`

第一次接 Rust（`crates/ime-post` + `crates/ime-py`）时，`maturin develop` 直接失败：

```
💥 maturin failed
Caused by: Failed to create temporary directory
Caused by: 拒绝访问。 (os error 5) at path "C:\...\Temp\.tmpXXXX"
```

换成 `cargo build` 编译能过，但链接阶段又会：

```
LINK : fatal error LNK1104: 无法打开文件"C:\...\Temp\lnk{GUID}.tmp"
```

两条报错看着无关，其实是同一个根因：**本机进程对默认 `%TEMP%` 没有完整写权限**
（普通 `echo > %TEMP%\x.txt` 能成，但需要显式安全描述符的创建会被拒）。
旁证：Python 的 `tempfile.gettempdir()` 在这台机器上直接回退到**当前工作目录**。

**解法**：构建时把 `TEMP`/`TMP` 指到 `D:\env\tmp`（`Authenticated Users: Modify`）。
`spike/tools/build_ext.py` 就是 `cargo build` + 把 `xifeng_rs.dll` 改名 `.pyd` 放进
site-packages —— 与 `maturin develop` 等价，但绕开它的临时目录步骤。maturin 仍在 dev
依赖里，给 CI / 无限制的机器打 wheel 用。

其他两条：

- **Rust `regex` 没有 lookaround**。Python 侧填充词边界是 `(?=[punct\s]|$)`，
  直接照搬会 `regex parse error: look-around ... is not supported`；
  改成 `([punct\s]|$)` + 替换时吐回 `$2`（"吞边界再吐回"），删除语义等价。
- **`\s` / `isspace` 的口径不完全一致**：Python `$` 还匹配结尾换行之前，Rust 只认
  haystack 末尾；Python `isspace()` 认 `\x1c`–`\x1f`，Rust 不认。前者靠右边界里带
  `\s` 兜住，后者 ASR 不会产生，暂不处理。









## G36 PyInstaller 排除「用不到」的模块，把整个核心打成 ImportError

**症状**：`build_core.py` 打包成功，但产出的核心一启动就死：

```
ModuleNotFoundError: No module named 'tkinter'
[PYI-39324:ERROR] Failed to execute script 'run_spike'
```

**根因**：为了减小体积加了 `--exclude-module tkinter`（IPC 模式确实不弹浮窗），
但 `xifeng/popup.py` 在**模块顶层**就 `import tkinter`，而 `run_spike.py`
无条件 `import popup`。于是不管走哪条路径，导入链都会踩到它。

**教训**：`--exclude-module` 只对「确认没有任何顶层 import 链能到达」的模块安全。
判断方法不是看当前模式用不用得到，而是看 `python -c "import run_spike"` 会不会失败。

**验证**：`uv run python tools/core_smoke.py` 直接跑打包产物，3 秒内就能发现。

体积代价：tkinter 约 7.5MB（118.8MB vs 111.2MB），完全不值得为它冒险。

## G37 `tauri build` 报「拒绝访问」：壳进程自己锁着自己的产物

**症状**：重新构建时报

```
error: failed to remove file `...\target\release\xifeng.exe`
Caused by: 拒绝访问。 (os error 5)
```

**根因**：上一轮验证时起的壳还在跑，运行中的 exe 文件被 Windows 锁住，
cargo 无法覆盖/删除它。与「构建前停托盘进程（占 `Xifeng.exe`）」是同一类问题——
**产物自己锁自己**，不是权限配置坏了。

**解法**：构建前先杀壳进程（Git Bash 里 `//` 是防止 MSYS 把 `/F` 当路径吞掉）：

```bash
taskkill //F //IM xifeng.exe
taskkill //F //IM Xifeng-Core.exe 2>/dev/null   # 壳拉起的核心也要一起收
```

**教训**：每次 `tauri build` / `build.py` 之前，先确认上一轮的产物进程已退出；
找不到占用者时用 `Get-Process | Where-Object { $_.Path -like '*target*' }` 按路径查。

## G38 NSIS 安装包报「Error writing temporary file」

**症状**：双击 `Xifeng_x.y.z_x64-setup.exe` 直接弹

```
NSIS Error: Error writing temporary file. Make sure your temp folder is valid.
```

注册表里 `%TEMP%` 指向正常、目录存在、C 盘空间充足——报错信息有误导性。

**根因**：NSIS 安装器启动时先把自己解包到 `%TEMP%\...\$PLUGINSDIR`。
这台机器的 `C:\Users\...\Temp` 目录 ACL 被多个沙箱 SID 污染过（G35 的旁证），
某些启动上下文下写不进去；杀软实时拦截 NSIS 自解包 exe 也会报同样的错。

**解法**（按顺序试，第 1 步通常就过）：

```bat
:: 0) 【2026-11-11 实锤的真根因】仓库目录带 Low 完整性标签（G24），
::    在 target\ 里产出的 setup.exe 继承 `Mandatory Label\Low (NW)`；
::    Low 进程写不进 TEMP / Program Files → 必报本错，换 TEMP 也没用。
::    修复：把安装器复制出仓库再装（Copy-Item 重建文件，标签随目标目录走）：
copy /Y target\release\bundle\nsis\Xifeng_0.1.0_x64-setup.exe D:\env\
::    复制后 icacls 应看不到 Mandatory Label 行，即可正常安装。
::    验证方法：icacls setup.exe —— 有 "Low Mandatory Level" 行就是带毒标签。
:: 1) 仍报错再指干净 TEMP
mkdir D:\env\tmp\nsis-install
set TEMP=D:\env\tmp\nsis-install
set TMP=D:\env\tmp\nsis-install
D:\env\Xifeng_0.1.0_x64-setup.exe

:: 2) 解除 Mark of the Web 后以管理员运行
powershell -c "Unblock-File 'D:\env\Xifeng_0.1.0_x64-setup.exe'"

:: 3) 查杀软拦截记录（Windows 安全中心 → 保护历史）
```

**静默模式的坑（2026-11-17 实测）**：在仓库 Low 目录里跑 `setup.exe /S`，
不给任何对话框，只返回 `exit=2`；`Start-Process` 甚至会一直挂住。
别把它当"脚本/钩子写错"——先 `Copy-Item` 到 `D:\env\build\` 再跑，`exit=0`。
（同一次还新增了 `nsis/hooks.nsh`，把默认安装位置挪到 `Programs\Xifeng`，
避开展开在用户数据目录里；细节见 packaging.md「交付」。）

**教训**：在这台机器上凡是「往 TEMP 解包/建临时文件」的工具
（maturin、link.exe、NSIS 安装器）都可能踩同一个 ACL 坑，
统一解法都是把 `TEMP`/`TMP` 指到 `D:\env\tmp`；
**交付物 exe 必须先离开 Low 标签目录再分发/运行**（G24 的又一实证）。

## G39 VAD 只留 60ms padding：Silero 起音延迟吃掉第一个音节

**症状**（2026-11-14，壳 cpal 采集 + `use_vad=1`）：按住说「你好喂，今天天气不错」，
注入的是「好喂今天天气不错」——**只有句首第一个字丢**，其余全对；长按提前开口也一样。

**先排除的**（都不是原因，别重复踩）：

| 怀疑点 | 对照结果 |
|---|---|
| 采集丢句首 | `in_frames` 与按住时长对得上（95840 帧 = 5.99s），会话音频里语音起点在 2900ms，流早已活着 |
| `trim_silence` | `trim` 开/关、整段、40ms 分批喂，全部输出完整的 9 字 |
| ASR 模型/解码 | 同一段音频离线跑 `use_vad=False` → 「你好喂今天天气不错」 |

**根因**：同一段音频跑 `use_vad=True` 精确复现 8 字。Silero 切出的首段是
`(3076ms, 5990ms)`，而 10ms RMS 显示真实语音从 **2908ms** 起振
（`…13,13,208,508,1004…`）——起音落在 VAD 边界之前 100–180ms。
Silero 需要连续 3 个窗 ≥ `start_prob` 才确认「开口」，确认点必然晚于真实起音；
而 `pad_ms=60` 只够盖住 60ms，第一个音节的声母（「你」的 n-）就被切掉了。

**解法**：`VadConfig` 加两个字段，**只作用于首/末段**：

```python
pad_ms = 60        # 内部边界：段间已有 merge_gap_ms(200) 保证，两侧各 60 不重叠
head_pad_ms = 300  # 首段起点额外前留（G39）
tail_pad_ms = 300  # 末段终点额外后留（韵母尾音）
```

内部边界 MUST 继续用 60ms：合并阈值 `merge_gap_ms=200` 是「段间距 < 200ms 就合并」，
若内部也扩 300ms，两个已合并段之外的边界会**重叠出重复音频**，反而诱发模型复读。

**验证**：

- 同段音频 `use_vad=True` → 「你好喂今天天气不错」（9 字）；首段多留 300ms 即可复原
- `cargo test -p ime-vad`：`head_pad_keeps_onset_before_first_active_frame`、
  `internal_boundary_pad_stays_small`
- `spike/tests/test_vad.py`：`test_head_pad_extends_first_segment`、
  `test_internal_boundary_pad_stays_small`；`test_vad_parity.py` 把 head/tail 纳入 kwargs
- e2e 实测：单击按住说两遍，`chars=9` × 2，`outcome=Injected`

**教训**：VAD padding 不是「越宽越安全」——**首末段和内部边界的语义相反**：
首末段是「宁可多留」，内部是「宁可少留」。改 VAD 参数必须跑 golden 用例，并让
Python 基准与 `crates/ime-vad` 同步（本例 Rust 侧同样加了两个字段与两个单测）。

## G40 热键：左右 Ctrl 互斥，组合键必须"完全不触发"

**症状**（2026-11-14，单击触发版）：

- 设置里选「左 Ctrl」后，按**右** Ctrl 照样冒浮窗——"设置哪一侧"形同虚设。
- `Ctrl+C` / `Ctrl+V` 会闪一下浮窗；播放空会话（空文本不注入，但麦克风被开了一下）。

**根因**：

1. 旧键位预设里有"左右均可"（`ctrl`/`shift` 映射到两个 vk）。用户库里存的正是
   `ctrl`，所以两侧都算数，选单侧也没用。
2. 单击触发是"按下即开会话"：浮窗、`asr_begin`、开麦都在按下瞬间发生，而组合键
   判定原本只在**录音循环**里做（`drain_presses`）——等它发现 C 已经按下，
   副作用早就发生了：组合键等于"触发了一个立刻作废的会话"。

**解法**：

- **预设只保留单侧**：`lctrl` / `rctrl` / `lshift` / `rshift`，默认**右 Ctrl**。
  旧值 `ctrl`→右、`shift`→左，在 `settings.load` 与 `KeyPreset::parse` 双端归一，
  老库不用重写、UI 显示真实生效的那一侧。
- **观察窗前移防线**：`COMBO_GUARD_MS=150`。按下后 150ms 内出现任何其它键
  （含按下**之前**就按住的 Shift/Alt——`Hook::down_except` 看的是"当前按住"，
  而 `drain_presses` 只看"新按下"）→ 静默丢弃：不弹浮窗、不发 `asr_begin`、
  立刻收流，等热键松开再等下一次按下。
- **不能因此丢句首**：开麦仍在按下瞬间，feeder 要等 `asr_begin` 之后才启动，
  观察窗里的音频就留在环形缓冲里，会话开始后一并送进 ASR。
- **按住时长从按下瞬间计**（含观察窗）。否则 300ms 的正常短句会被算成 150ms
  < `MIN_HOLD_MS(250)`，误触判定反而误伤。

**验证**：`cargo test`（壳）15 绿，其中 `presets_are_side_exclusive`、
`parse_maps_legacy_values_to_one_side`；pytest 的
`test_hotkey_sides_are_exclusive`、`test_legacy_hotkey_value_normalized_to_one_side`。

**教训**：触发方式从"双击（天然过滤组合键）"换成"按下即触发"后，组合键的防线
必须**前移到触发之前**；放在录音循环里只能补救输出，补不了已经发生的副作用
（浮窗、开麦、ASR 会话）。

## G41 浮窗"等半天才出来"不是麦克风授权，是 WASAPI 建流（且随空闲时长变贵）

**症状**（2026-11-15 用户提问：「这个输入提示框是等麦克风授权了才弹出来的吗」）：
按住热键后浮窗要等一下才出现；**启动后第一次**尤其明显；而"刚说完一句马上再说"几乎立刻。

**先排除一个误会**：Windows **没有**"按应用的麦克风授权弹窗"。麦克风隐私开关是系统设置里
一次性打开的项（`设置 → 隐私 → 麦克风`），本机从未弹过授权框，日志里也没有拒绝错误。
所以"等"的绝不是权限。

**实测拆解**（便携壳 + `SendInput` 合成按键，`start()` 内分段计时 + 外部 `Stopwatch` 观测窗口）：

| 阶段 | 空闲 ≤4s | 空闲 ≥6s | 启动后第一次 |
|---|---|---|---|
| `default_input_device()` + `default_input_config()` | 3–11ms | 5–11ms | 30–249ms |
| `build_input_stream()`（`IAudioClient::Initialize`） | **73–150ms** | **454–533ms** | **671–868ms** |
| `stream.play()` | 0ms | 0ms | 0ms |
| 150ms 组合键观察窗（G40）剩余部分 | ~45ms | ~0ms | ~0ms |
| `overlay.show()` 本身 | 0ms | 0–1ms | 0–1ms |
| **按下 → 浮窗可见** | **161–192ms** | **~510ms** | **~910ms** |

**为什么跟空闲时长强相关**：Windows 在最后一个采集流关闭约 5 秒后回收音频引擎
（APO/端点管线一起拆），下次 `Initialize` 要把它们重新拉起来——这就是那 450–500ms。
实测（同一台机，每轮先说一句再等特定时长）：

| 距上一句的间隔 | `build_input_stream` |
|---|---|
| 0s / 3s | 149 / 118ms |
| 6s / 8s / 10s / 15s / 25s | 480 / 454 / 496 / 481 / 533ms |

**试过但无效：启动时预热一次**（`Mic::warmup()`：开流→立刻 `stop_stream()`）。
预热本身耗时 ~1s，但用户按下通常已是几十秒后，引擎早被回收——实测预热后 12 秒再按，
`build_input_stream` 仍是 **805ms**。所以预热只解决了"启动后 5 秒内就按"这种罕见情况，
白占 0.7s 麦克风，**已撤回**（D19）。

**结论 / 现状**：保持"按下才开流、松手就关"（不常驻麦克风，隐私优先）。
代价是浮窗（= 麦克风真正开始采集的时刻）在空闲后要等 ~0.5s；这期间说的话确实没被采到，
VAD 的 `head_pad_ms` 也救不了（那段音频根本不存在）。要彻底消除只能让流常驻
（麦克风指示灯常亮），用户明确不选。

**教训**：
1. 报"打开麦克风耗时"必须**分段计时**（枚举 / 读格式 / 建流 / play），否则会把
   `IAudioClient::Initialize` 的代价误判成"权限/授权"，去翻隐私设置一通白找。
2. G23 里 `play→首回调 17–24ms` 只覆盖建流**之后**那一段；不能拿它当"按下即开流 =
   零等待"的依据。
3. 浮窗晚弹**不是**解决"用户对着没开的麦克风说话"的好办法——那是用响应速度
   换一个用户根本看不懂的停顿（见 G42/D20：已改为弹窗与开流并行，浮窗 150ms 就出，
   冷态下"浮窗已出但还没开始采"的 300–650ms 明确记账）。

## G42 弹窗与开流 MUST 并行：串行会把 G41 的建流耗时直接摆到用户脸上

**症状**（2026-11-16 用户提问：「就不能按下 ctrl，输入提示弹窗和拉取麦克风同时开启？」）：
浮窗有时要等半秒才出现——不是权限（G41），而是**实现上把两件事串起来了**：
`wait_for_press` 里先 `mic.start()`（阻塞，冷态 450–800ms），返回后才 `show()`。

**为什么不能"把开流丢到别的线程"**：`cpal::Stream` 是 **`!Send`**
（`platform/mod.rs`：`pub struct Stream(StreamInner, NotSendSyncAcrossAllPlatforms)`），
流必须在创建它的线程里持有。所以并行的是**弹窗这一路**，不是开流那一路。

**实现**：按下瞬间起一个弹窗线程，等到 `t_down + COMBO_GUARD_MS` 后自己判一次
组合键（`down_except` + `drain_presses`）与是否仍按住；通过就 `emit(theme) + show()`。
主线程同一时刻 `mic.start()`。两条线的汇合点在 `popup.join()`：弹窗线程的
`drain_presses` 是**取走**语义，它判定出的"组合键"必须回传，否则主线程会漏判。

**实测（便携壳 + SendInput，外部 `Stopwatch` 观测窗口可见）**：

| 场景 | 改前（串行） | 改后（并行） |
|---|---|---|
| 启动后第一次（冷） | 911ms | **211ms** |
| 空闲 15s（冷） | ~510ms | **168ms** |
| 紧接着（热） | 161–192ms | **177ms** |

组合键回归：右 Ctrl+F24、先按左 Shift 再按右 Ctrl、单独左 Ctrl —— 全程 2ms 级盯窗，
**浮窗一次都没闪过**，也没有会话日志。

**代价（必须记账）**：冷态下浮窗比"麦克风真正开始采集"早 300–650ms。用户若一看
浮窗就立刻开口，开头那一段仍然没被采到（G41 的物理限制，弹窗快了也改不了）。
要彻底消除只能让流常驻（D19 已否决：用户选"不占麦"）。

**教训**：
1. 交互里"慢"要先分清是**物理成本**还是**串行浪费**。这里 450–800ms 是物理成本，
   但把它摆在 `show()` 前面是纯粹的实现浪费——并行后用户感知直接回到观察窗的 150ms。
2. 跨线程共享"按键判定"时，注意 `drain_presses` 的取走语义：判定结果要么回传，
   要么只让一个线程消费，否则组合键会漏判（G40 的防线会静默失效）。

## G43 改名别只改字符串：`%LOCALAPPDATA%` / 注册表 / sidecar 名都是用户数据契约

**背景**（D21，2026-11-16 用户决策）：项目由 `VoiceIME` 全量改名为 **Xifeng**（溪风）。
改名看起来是查找替换，实际有三类"看不见的契约"：

1. **磁盘路径**：`%LOCALAPPDATA%\VoiceIME\{xifeng.db,models,*.log}` —— 直接改默认值，
   老用户的历史、设置、228MB 模型全部"消失"（应用会安静地新建一个空库）。
2. **注册表**：`HKCU\...\Run\VoiceIME` —— 只改新值名，旧值还在，开机跑两个实例。
3. **sidecar 名**：壳按文件名认 Python 核（`Xifeng-Core.exe`），改名后路径找不到，
   表现为"启动即无语音"，且错误只在一行日志里。

**映射表（一次改全）**：

| 层 | 旧 | 新 |
|---|---|---|
| 产品/显示名 | VoiceIME | **Xifeng**（溪风） |
| Tauri productName / identifier | VoiceIME / com.voiceime.app | Xifeng / com.xifeng.app |
| 壳 exe | voiceime-tauri.exe | xifeng.exe |
| Python 核 | VoiceIME-Core | Xifeng-Core |
| 数据目录 | `%LOCALAPPDATA%\VoiceIME` | `%LOCALAPPDATA%\Xifeng` |
| SQLite / 日志 | voiceime.db / voiceime.log | xifeng.db / xifeng.log |
| 自启值 | `Run\VoiceIME` | `Run\Xifeng` |
| Python 包 / PyO3 模块 | voiceime_spike / voiceime_rs | xifeng / xifeng_rs |
| 环境变量 | `VOICEIME_*` | `XIFENG_*`（旧名在壳里仍兼容读取） |

**迁移规则（`paths.migrate_legacy_data`，入口显式调用）**：

- `IME_DATA_DIR` 显式指定时**不动**（用户已接管位置）
- 新目录不存在 → 整目录 `rename`（同卷瞬时，模型不用复制 228MB）
- 新目录是空壳（`data_dir()` 的 `_usable()` 可能先建出来）→ 删空壳再 rename
- 新目录已有数据 → 只补旧目录里**缺的**条目，同名一律不覆盖
- 库/日志改名时带上 `-wal`/`-shm`；`voiceime.db` 已改为 `xifeng.db`
- **任何一步失败只告警不拦启动**；`db_path()` 与 `models_root()` 仍会回退到旧目录，
  保证"迁移失败 ≠ 历史/模型消失"

**为什么迁移不放进 `data_dir()`**：路径函数是纯查询，打包脚本、测试、`--autostart`
都会调用它；在里面偷偷改磁盘会让"跑一次构建"就搬走用户数据。迁移必须由 `main()`
显式调用（`run_spike.py` 已接），且幂等。

**验收**：`tests/test_paths.py` 覆盖整目录接管、空壳、已有新库不覆盖、显式
`IME_DATA_DIR` 跳过、迁移失败回退旧库/旧模型；真实机器上旧目录含 228MB 模型时
启动一次即完成（日志 `[paths] 已迁移旧版数据 → %LOCALAPPDATA%\Xifeng`）。

**踩坑（2026-11-17 实测）**：在仓库目录里拿 `spike/.venv/Scripts/python.exe` 手动跑迁移，
会把 `rename` 变成 `PermissionError(13, '拒绝访问')`。根因不是代码：仓库带 **Low 完整性标签**
（G21/G24），venv 里的 `python.exe` 继承了这个标签，`icacls` 可见
`Mandatory Label\Low Mandatory Level:(I)(NW)`；于是该进程**只能写 Low 对象**，
对 `%LOCALAPPDATA%`、`D:\env\*`、`%TEMP%` 一律 `EACCES`（同一个路径用 PowerShell / 系统
Python 就正常）。所以迁移/落盘类验证 MUST 用 **复制到仓库外再运行的产物**
（`resources/core/Xifeng-Core` → `D:\env\tmp\...`，robocopy 到 Medium 目录后标签即脱落），
或用仓库外的解释器；不要拿 venv 里的 Python 直接验证"能不能写用户目录"。

**踩坑（2026-11-17 二次实测）**：迁移日志写在 `ipc_mode()` 之前，而 Windows 下
stderr 默认是 gbk；壳按 UTF-8 逐行读，第一行中文就 `stream did not contain valid UTF-8`，
**整条 sidecar 诊断管道当场断流**（语音本身不受影响，但后续错误全看不见）。
修法：`main()` 里先看 `args.ipc`，把 `sys.stderr` 提前 reconfigure 成 UTF-8，
不能等进 `ipc_mode()` 才设。

**教训**：改名要先列"用户数据契约清单"（磁盘/注册表/进程名/环境变量/安装器/日志编码），
再动字符串；只改显示名的替换是假改名。
