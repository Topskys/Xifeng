# 快速开始

本文件只回答一件事：**怎么在这台机器上，5 分钟内把一次语音输入跑通。**

当前交付形态是 Python + PyInstaller（`plan.md` D6），可运行的只有 `spike/`。
GUI（M3）与托盘/开机自启（M4）已实现；仍是**单份 console 产物**，`--tray` 启动时
自己隐藏控制台窗口（`plan.md` D7）。

## 0. 前提

| 项 | 要求 | 检查 |
|---|---|---|
| 系统 | Windows 10/11 x64 | — |
| uv | ≥0.11 | `uv --version` |
| 麦克风 | 任意可用输入设备 | `uv run python -c "import sounddevice;print(sounddevice.query_devices())"` |
| 磁盘 | ≥1GB（模型 239MiB + 环境） | — |

> Rust 工具链（`cargo`）**M0 不需要**，Step 1 才装。

## 1. 装环境（约 20 秒）

```bash
cd voice-ime/spike
uv sync
```

`uv` 会创建 `spike/.venv`（Python 3.11），并按 `pyproject.toml` 从清华镜像装依赖。

## 2. 验证代码没坏（<1 秒）

```bash
uv run pytest
```

期望：`193 passed`。

## 3. 先看文本清理（不需要模型、不打字）

```bash
uv run python run_spike.py --text "呃 今天 我们 嗯 开个会，讨论一下 PyO3。" --no-inject
```

期望输出：

```
[post] raw='呃 今天 我们 嗯 开个会，讨论一下 PyO3。'
[post] cleaned='今天 我们 开个会，讨论一下 PyO3。'
```

纠错默认关，`--text` 不会调用模型。要试本地模型纠错（首次约 4s 冷启动；模型放
统一根 `%LOCALAPPDATA%\Xifeng\models\llm`，见 packaging.md 的 D15 节）：

```bash
> uv run python run_spike.py --text "帮我把 voice i e 的 read me 提交" --correct --no-inject
[correct] applied fixes=1 3515ms
[post] raw='帮我把 voice i e 的 read me 提交'
[post] cleaned='帮我把 Xifeng 的 README 提交'
```

模型改坏时会回退原文并打印原因（G32）：

```
[correct] 保留原文（drop-start）4288ms
```

## 4. 下载模型（239MiB，可选）

不下载也能跑，ASR 会退回 `mock`（固定文本）。要真实识别就下载：

```bash
uv run python fetch_model.py
```

支持断点续传；默认走 `hf-mirror.com`（官方源国内不通）。

## 5. 录一段，看 Trace

**按住说话（推荐，和最终形态一致）**：

```bash
uv run python run_spike.py --ptt --no-inject --print-raw
```

- 看到 `等待按下 ctrl …` 后，**按住 Ctrl 说一句，松开**
- 启动时会打印 `[mic] 按需开麦`：不按键时麦克风是关的，按下才开始采集（G23）
- 代价是按下后 100–400ms 的开流静默期：浮窗/提示出现后再开口，别抢话
- 确实需要消掉这段静默期才加 `--resident-mic`（会一直占用麦克风，Windows 指示常亮）
- `[mic] 2.15s` 比按住时长长是正常的（含前置缓冲 + 120ms 尾音收尾）
- `--no-inject` 只处理、不打字，安全
- 松开后 250ms 内不足判定为误触（`Ctrl+C` 不会触发）；录音中按 Esc 取消
- 默认会**裁掉首尾静音**（省 ASR 时间、防长静音幻觉）；想听原样加 `--no-trim`

连说 3 次验证稳定性：

```bash
uv run python run_spike.py --ptt --times 3 --no-inject
```

`--times 0` 表示一直等（`Ctrl+C` 结束），托盘模式默认就是这个值。

**注入到不吃 `WM_CHAR` 的应用**（少数终端、DirectInput 游戏）：

```bash
uv run python run_spike.py --ptt --inject clipboard   # 剪贴板 + Ctrl+V，最兼容
uv run python run_spike.py --ptt --inject unicode     # 只发按键，不碰剪贴板
uv run python run_spike.py --ptt --inject auto        # 默认：先按键，确认没打进去才兜底
```

`clipboard` 会在粘贴后把剪贴板恢复成原内容（默认等 500ms，`--clipboard-restore-ms`
可调）；如果你在此期间复制了新东西，它不会覆盖。`auto` 只对**能读出文本**的输入框
（Edit / RichEdit / Scintilla）生效，控制台/游戏请显式用 `clipboard`。

**按住 Ctrl 没反应？** 先跑 `uv run python key_diag.py`（不占麦克风、不加载模型）。
它会打印完整性级别、钩子事件计数、Tk 窗口能否收键，结论区直接给下一步。

> ⚠ 本工作区 `D:\project\workspace` 带 Low 完整性标签，目录内的 exe 都是 `Low(0x1000)`，
> 全局钩子必然 0 事件（`05-gotchas.md` G21）。进程完整性只看 exe，不看 CWD，
> 所以新建一个目录外的 venv 就能绕开：
>
> ```bash
> uv venv %TEMP%\vi-venv --python 3.11
> set PYTHONPATH=.venv\Lib\site-packages
> %TEMP%\vi-venv\Scripts\python.exe key_diag.py          # 应为 Medium(0x2000)
> %TEMP%\vi-venv\Scripts\python.exe ptt_selftest.py      # 不需要人按，4/4 即 PTT 链路正常
> %TEMP%\vi-venv\Scripts\python.exe run_spike.py --ptt --backend mock --no-inject
> ```

换键位：`--key rctrl`（只认右 Ctrl，避开快捷键）、`--key rshift`（几乎无冲突）。

回车版本（不占键盘，适合调试）：`uv run python run_spike.py --interactive --no-inject`

## 6. 真注入（会往当前光标打字）

最省事的验证：让脚本自己开一个测试窗口、注入、再读回比对。

```bash
uv run python inject_smoke.py
```

期望 `5/5 通过`（中文、中英混排、emoji、换行、长文本），每行带 `inject=XXms`。
**必须在自己的终端里跑**：自动化/后台 shell 抢不到前台窗口（`docs/05-gotchas.md` G14）。
默认 `chunk=32` 已验证通过；若某台机器丢字，用 `--chunk 8` 复测。

如果想打到记事本里：

```bash
uv run python run_spike.py --ptt
```

终端显示 `等待按下 ctrl …` 后，**按住 Ctrl 不放**，用鼠标点一下记事本（Ctrl 一直按着，录音不会中断），
说完松开 Ctrl，文字落在记事本光标处。或者用回车版本：

```bash
uv run python run_spike.py --text "你好，世界😀"
```

执行后立刻点回记事本，注入会落在记事本的光标处。

> ⚠️ 焦点在终端时，注入的文字会打进 shell 提示符。首测请加 `--no-inject`。

## 7. 拿延迟数字

```bash
uv run python bench.py --n 20 --seconds 3                                   # 管线开销
uv run python bench.py --n 20 --wav sample.wav --backend sherpa --threads 4 # 真实 ASR
```

结果追加到 `../docs/01-overview/latency-budget.md` 的"当前实测"表，禁止覆盖历史行。

## 8. 验证"认得准不准"

延迟达标 ≠ 能用。跑固定句子，算字错率：

```bash
uv run python quality_check.py
```

- 屏幕会逐句显示要读的句子：按回车开始，读完再按回车
- 结果：每句 CER + 总体判定（≤5% 合格 / ≤15% 可接受 / >15% 有问题）
- 默认量**注入前的最终文本**（清理 + 归一化）；`--no-refine` 只看 ASR 原始输出
- `--correct` 才把模型纠错计入 CER（与运行时默认一致：默认关）
- 录音存 `fixtures/quality/01.wav…05.wav`，可复算：`uv run python quality_check.py --wav-dir fixtures/quality`

> 必须用真人录音。TTS 合成音频对 SenseVoice 太友好，CER 接近 0，不代表真实分布。
> 当前 5 句：原始 3.3%，清理后 0.0%（规则层下线后仍为 0.0%，见 `04-reference/metrics.md`）。

## 9. 看历史 / 统计 / 改设置

数据都在一个 SQLite 文件里（`%LOCALAPPDATA%\Xifeng\xifeng.db`；
Low 进程回退 `spike/.data`）。见 `03-core-concepts/storage.md`。

```bash
uv run python run_spike.py --stats              # 次数 / 成功率 / P50 / P95
uv run python run_spike.py --history 20         # 最近 20 条
uv run python run_spike.py --no-db --text "..."  # 完全不碰数据库
uv run python run_spike.py --no-store-text       # 历史只留长度，不落文本
```

图形界面（Web，五页：统计 / 模型 / 配置 / 历史 / 关于）：

```bash
uv run python run_spike.py --web                 # 默认打开统计页
uv run python run_spike.py --web --tab history   # 直接开历史页
uv run python run_spike.py --web --tab config    # 纠错开关、清理规则、注入方式
```

Web 界面是**独立进程**：本地只监听 `127.0.0.1:8642`，用系统 Edge 的 `--app` 打开
无地址栏窗口；窗口关闭 30s 后服务自动退出。前端已构建到 `frontend/dist`，
改了 `frontend/src` 要重新 `vite build`（见 `05-gotchas.md` G31）。
只读同一个 SQLite；设置改完要重启托盘进程才生效。

> `--gui` 仍能打开旧版 tkinter 四页窗口（无「关于」页），仅作兜底；
> 托盘菜单与日常使用都走 Web。

## 10. 托盘常驻 + 开机自启（日常形态）

```bash
uv run python run_spike.py --tray                # 隐藏控制台，托盘常驻
uv run python run_spike.py --autostart status    # 查看是否开机自启
uv run python run_spike.py --autostart on        # 写入 HKCU\...\Run（免 UAC）
uv run python run_spike.py --autostart off
```

- 托盘右键：打开主窗口 / 统计 / 模型 / 配置 / 历史 / 关于 / 开机自启 / 退出
- `--tray` 等于 `--ptt --times 0`：一直等热键，直到菜单「退出」；默认按需开麦，不占设备
- 菜单里的页面都开 Web 窗口（`--web --tab`）；已有服务时只开窗口，不重复监听端口
- 控制台被隐藏后，日志在 `%LOCALAPPDATA%\Xifeng\xifeng.log`
  （源码 Low 进程回退 `spike/.data/xifeng.log`）
- 只允许一个托盘实例；重复启动会直接退出（命名 Mutex）

> 开机自启命令带 `--tray`。源码形态写的是
> `"<python.exe>" "<spike>\run_spike.py" --tray`；冻结形态写的是 `"Xifeng.exe" --tray`。
> 若移动了程序目录，先 `--autostart off` 再 `on` 一次。

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `[asr] sherpa-onnx 未安装，退回 mock` | 依赖没装 | `uv sync` |
| `模型目录不存在，退回 mock` | 没下载模型 | `uv run python fetch_model.py` |
| `CERTIFICATE_VERIFY_FAILED` | hf-mirror 证书过期 | 用 `fetch_model.py`（走 curl/schannel），别用 urllib |
| `PortAudioError: No Default Input Device` | 没麦克风/被占用 | 插设备；或在声音设置里启用 |
| `outcome=Failed error=ElevatedTarget` | 目标窗口是管理员权限 | 以管理员身份运行，或换普通窗口 |
| `outcome=Failed error=FocusLost` | 执行期间焦点变了 | 加 `--restore-focus`，或别切窗口 |
| 注入成功但没字 | 目标应用不吃 `WM_CHAR`（部分游戏/终端） | 换 `--inject clipboard`；默认 `auto` 只对能读文本的输入框自动兜底 |
| 中文变乱码 | 用了 `chars()` 而不是 UTF-16 码元 | 看 `inject.build_events` 的单测 |

## 下一步

- 要交付给不装 Python 的人：`02-guides/packaging.md`（PyInstaller onedir）
- 要加功能：先看 `00-process/plan.md` 的 Step 0 表与 `00-process/sdlc.md`（闸门）
