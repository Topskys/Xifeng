# 打包与交付

**当前交付形态：Tauri 壳（`xifeng.exe`）+ Python sidecar（`Xifeng-Core`）**（D14/D16）。
壳与安装器在 `apps/desktop` 用 `pnpm tauri:build` 构建；sidecar 由
`packaging/build_core.py` 打进 `src-tauri/resources/core/`。见下文「sidecar」与「发版」两节，
工具链见 `tauri-env.md`。

> 下面「前置 → 交付」的主体是 **PyInstaller 形态**（D6 时代产物，仅用于单独交付
> Python 运行时），保留是因为 `packaging/build.py` 仍在用；日常发版走壳路线。

## 前置

```bash
cd core
uv sync          # 已包含 dev 依赖 pyinstaller>=6.11
```

无需 MSVC / gcc / rustup。

## 构建

⚠ **必须用 Medium 完整性进程构建**。本仓库目录（`D:\project\workspace`）带 Low 标签，
目录内的 `.venv\Scripts\python.exe` 也是 Low：

- Low 进程写不进 `%TEMP%` / `%LOCALAPPDATA%`（Medium 目录，no-write-up），
  `tempfile.gettempdir()` 会**静默退回当前目录**；
- 输出目录的标签决定 exe 的完整性级别 —— 落在仓库里的 exe 是 Low，全局钩子收不到硬件按键（G21/G24）。

在普通（非 agent）终端里用目录外的解释器构建：

```bat
cd /d D:\project\workspace\Xifeng\core
uv venv %TEMP%\vi-venv --python 3.11
set PYTHONPATH=D:\project\workspace\Xifeng\core;D:\project\workspace\Xifeng\core\.venv\Lib\site-packages
%TEMP%\vi-venv\Scripts\python.exe packaging\build.py
```

产物：

```
%LOCALAPPDATA%\Xifeng-build\dist\Xifeng\
├── Xifeng.exe          5.5 MB
├── Xifeng-Tray.cmd     双击启动托盘（等价于 Xifeng.exe --tray）
└── _internal\            ~140 MB（python311 / onnxruntime / openblas / portaudio / sqlite3 / tkinter / pystray …）
```

模型默认**不打进包**（ASR 228MB + 纠错 491MB，单独升级更快）。

> ⚠ PyInstaller `--noconfirm` 会**重建整个 `dist\Xifeng` 目录**：上次放进去的
> `models\` 会被删掉（构建结束会打印提示）。用户数据不在这里 —— 库默认在
> `%LOCALAPPDATA%\Xifeng\xifeng.db`，重建不会丢（见 G25）。

## sidecar：Tauri 内嵌核心（build_core.py）

`packaging/build_core.py` 把 Python 核心打成 **onedir sidecar 供 Tauri 壳拉起**
（与 `build.py` 的"独立可双击产品"不同，它不带 GUI，只跑 `--ipc`）：

```bash
cd core
uv run python packaging/build_core.py          # 产出 apps/desktop/src-tauri/resources/core/
uv run python packaging/build_core.py --clean  # 先清掉旧产物再打
uv run python tools/core_smoke.py              # 独立冒烟：ready→bootstrap→stats→shutdown
```

要点：

- 产物落在 `apps/desktop/src-tauri/resources/core/`，`tauri.conf.json` 的
  `resources` 把 `resources/core/` 映射到安装目录 `core/`，约 100MB+，
  **已在 `.gitignore`，绝不入库**（G24 同理）。
- `tauri` 的 `py.rs` 按 `is_sidecar` 判断：进程名是 `Xifeng-Core.exe` 时直接执行、
  不再传 `run_spike.py`；解释器模式才传脚本名。数据库可用 `--db` 覆盖。
  sidecar 按以下顺序找：`XIFENG_PYTHON` 环境变量 →
  `<exe 同级>\core\Xifeng-Core\Xifeng-Core.exe`（onedir）→ 平铺历史候选 → PATH `python`。
  **布局 MUST 与 py.rs 候选一致**——onedir 是 `core\Xifeng-Core\` 子目录（带 `_internal`），
  曾因写成平铺导致壳找不到 sidecar、静默跌到 PATH python。
- **冒烟 MUST 在 `tauri build` 之前跑**（G36 就是冒烟发现的：3 秒内报
  `ModuleNotFoundError: tkinter`，而打包本身是成功的）。
- **CRT 版本覆写（G33 sidecar 版）**：PyInstaller 会从依赖收进旧
  `MSVCP140.dll`（实测 14.31），与 llama-cpp-python 的 14.40 工具链 DLL 混用
  会在冻结态 `llama_backend_init()` 直接 `access violation reading 0x0`——
  **只在冻结形态复现**（venv 里连打包目录的 llama.dll 都加载正常）。
  `build_core.py` 已移植 build.py 的 `fix_msvc_runtime`：打完自动按版本号
  用系统新副本覆盖 `_internal` 里的 CRT。验证方式：打包 core 独立跑
  `--ipc` 喂 `correct`（UTF-8 字节经 cmd 重定向直灌，PowerShell 管道会把
  中文编码成 `?`），日志应出现「correct 模型已热身」且推理返回真实 ms。
- 端到端验证（壳 → sidecar → IPC）用 `tools/ask_boot.py <port>`：窗口开调试端口后
  经 CDP 调 `py_call("bootstrap")`，返回 `OK {...version...}` 即通。
- 实测：sidecar 启动到 ready 约 403ms，IPC 冒烟退出码 0。

## 模型放哪（D15，2026-11-11 统一根）

**模型只有一个存放点**：`%LOCALAPPDATA%\Xifeng\models\{asr,vad,llm}`
（即 `data_dir()/models`）。开发、调试、打包后的程序都从这一处读；
仓库里**不再放模型**（原 `models/` 已移除）。`IME_MODELS_ROOT` 可整体换根
（便携 U 盘 / 自定义盘符）。

各模型路径（`xifeng/paths.py`，解析顺序一致）：

| 模型 | 环境变量（整体覆盖） | 统一根位置 |
|---|---|---|
| ASR（目录） | `IME_MODELS_DIR` | `models\asr\sensevoice-small-int8` |
| VAD | `IME_VAD_MODEL` | `models\vad\silero_vad.onnx` |
| 纠错 LLM | `IME_LLM_MODEL` | `models\llm\qwen2.5-0.5b-instruct-q4_k_m.gguf` |

解析顺序（三类一致）：环境变量 → 统一根 → **旧布局兼容兜底**
（打包态 `<exe 同级>\models\…` → 源码态 `<仓库根>\models\…`，只在已有模型时生效）。

注意：统一根的探测是**只读**的——Low 完整性进程（沙箱调试）写不进
`%LOCALAPPDATA%` 但读得到（G25 是写数据的规矩），所以沙箱里也能正常加载模型。

**安装器默认不带模型**（2026-11-11 用户决策）：安装器只含核心（约 34 MiB），
运行时从统一根 `%LOCALAPPDATA%\Xifeng\models` 读模型。要出「离线开箱即用」
包时才 `uv run python packaging/build_core.py --with-models`（统一根 →
`resources/models/` → 安装目录 `models/` 就地兜底）。LLM 缺失不致命：
配置页显示"模型缺失"，按原文注入。

## 数据与命令（打包后同样可用）

| 数据 | 位置 |
|---|---|
| 历史 / 统计 / 设置 | `%LOCALAPPDATA%\Xifeng\xifeng.db`（不可写时回退 `<exe 同级>\data`） |
| 模型（ASR/VAD/LLM 统一根，D15） | `%LOCALAPPDATA%\Xifeng\models\{asr,vad,llm}`；离线包自带 `models/` 兜底 |
| 纠错模型（可选） | 统一根 `models\llm\qwen2.5-0.5b-instruct-q4_k_m.gguf` 或 `IME_LLM_MODEL` |
| 托盘日志 | `%LOCALAPPDATA%\Xifeng\xifeng.log`（只记长度/结果/trace，不记识别全文） |
| 音频 | 从不落盘 |

```bat
%EXE% --stats                 :: 次数 / 成功率 / P50 / P95
%EXE% --history 20            :: 最近 20 条（含注入文本）
%EXE% --web                   :: 主窗口（五页：统计/模型/配置/历史/关于）
%EXE% --web --tab config      :: 直接开配置页（模型纠错开关在这里）
%EXE% --tray                  :: 托盘常驻（隐藏控制台，无限 PTT，按需开麦）
%EXE% --autostart on          :: 开机自启（HKCU\...\Run，带 --tray）
%EXE% --autostart status
%EXE% --autostart off
%EXE% --tray --inject clipboard  :: 目标不吃 WM_CHAR 时改走剪贴板 + Ctrl+V
%EXE% --text "呃 帮我把 voice i e 的 read me 提交" --no-inject
:: → 帮我把 voice i e 的 read me 提交（纠错默认关）
%EXE% --text "帮我把 voice i e 的 read me 提交" --correct --no-inject
:: → 帮我把 Xifeng 的 README 提交（首次冷启动 +4s）
```

`sherpa_onnx` 的 DLL 由 `--collect-all` 收进 `_internal`；`sqlite3` 随 Python 自带；
`pystray`/`Pillow`（托盘）由 PyInstaller 的 hook + `--collect-all` 收进包内；
`llama_cpp`（纠错运行时，含 `llama.dll`/`ggml*.dll`）同样用 `--collect-all`。
`tkinter` 仍随包（`--gui` 是旧版兜底），主界面是 Web：`build.py` 把 `apps/desktop/dist`
复制成 `<exe 同级>/frontend`，所以交付目录 MUST 连同 `frontend` 一起分发。
**纠错模型**（D15 统一根）：优先放 `%LOCALAPPDATA%\Xifeng\models\llm`
（重建 dist 不丢）；打包自带时在安装目录 `models/llm/` 兜底；缺失时配置页显示
"模型缺失"并给出目录，开关禁用，语音输入照常（只是没有纠错）。

**运行库（CRT）**：`build.py` 末尾会跑 `fix_msvc_runtime()` —— 用 `System32` 里更新的
`msvcp140`/`vcruntime140`/`vcruntime140_1`/`concrt140` 覆盖 `_internal` 里的旧副本。
PyInstaller 会从某个依赖里收进旧 `msvcp140.dll`（实测 14.31），而 Windows 优先加载 exe
同目录的副本，于是顶掉系统的 14.42；llama.cpp 的 DLL 是 14.40 工具链构建的，混用旧 CRT
会让 `--correct` 崩在 `llama_backend_init()`（`access violation reading 0x0`，G33）。
**不要用"删掉这个 DLL"来修**，目标机可能没装 VC redist。

## 实测（2026-10-29，本机 8 核）

| 项 | 数字 |
|---|---|
| 构建耗时（`--clean`） | 34.4s（PyInstaller 6.22.3） |
| 程序体积（不含模型） | 145.7 MB / 1228 个文件；exe 5.5 MB |
| 交付体积（含模型） | 374.1 MB / 1230 个文件 |
| `--text` 冷启动 | 1.98s（bootloader + numpy 导入） |
| `--wav`（4.61s 音频） | 全程 3.67s，其中 ASR 352ms；模型加载约 2.6s（一次性） |
| `--wav`（6.53s 音频） | ASR 466ms，e2e 467ms |
| `--wav`（8.00s 音频，带数据层） | ASR 537ms，`fixes=2`（当时规则层），历史正常写入 |
| `--ptt`（自注入 Ctrl 1.5s） | 收 1.94s，`1/1 次注入成功`，`完整性=Medium(0x2000)` |
| `--ptt --no-inject`（**物理键**，16.6s 音频） | 钩子收到按键，`mode=ptt outcome=Injected`，ASR 1190ms / e2e 1191ms |
| `--ptt`（**物理键 + 真注入**，7.4s 音频） | `strategy=unicode`，ASR 567ms + 注入 21ms，**e2e 588ms**，文字落在前台窗口 |
| `--wav`（4.61s + 11s 静音） | 裁掉 12.1s，ASR **283ms**（未裁时 552ms）；5 句带裁剪 CER 仍 **0.0%** |
| `--tray`（冻结 exe） | 控制台 `hwnd=0`（已隐藏），日志落 `%LOCALAPPDATA%\Xifeng\xifeng.log`，`完整性=Medium(0x2000)` |
| `--gui`（冻结 exe，`CREATE_NO_WINDOW` 拉起） | 旧版 tkinter 四页兜底，读真实库 `%LOCALAPPDATA%\Xifeng\xifeng.db`；日常入口是 `--web` |
| `--autostart on/off`（冻结 exe） | 写入/删除 `HKCU\...\Run\Xifeng = "...\Xifeng.exe" --tray` |
| `--tray --inject clipboard`（跨进程 WinForms `Edit`） | 注入 **507ms**（含 500ms 恢复等待），文本落地，剪贴板还原为注入前哨兵 |
| `--inject auto`（同一目标） | **28–39ms**；Unicode 成功即返回，不碰剪贴板；探测到没打进去才降级 |

启动 1.98s 在 spec 的 <5s 内；ASR 数字与源码形态一致（打包不引入推理开销）。

### 2026-10-30 复测（含本地纠错）

| 项 | 数字 |
|---|---|
| 交付体积（ASR 模型随包，纠错模型不进包） | 392.2 MB / 1294 个文件；exe 6.3 MB |
| 纠错模型（单独放数据目录） | 469 MB → `%LOCALAPPDATA%\Xifeng\models\llm` |
| `--wav fixtures/quality/01.wav`（纠错关，`sherpa`） | ASR 331ms，e2e **353ms** |
| 同上 + `--correct` | 模型加载 + warmup 1855ms（一次性），纠错 693ms，e2e **1047ms** |
| `--text "…read me…" --correct`（G33 修好后） | `[correct] applied fixes=1 519ms` |
| 冻结包 `--correct` 崩溃 | 已修：旧 `msvcp140` 顶掉系统版（G33） |
| 托盘开启纠错启动 | `[correct] qwen2.5-0.5b-instruct-q4_k_m 已加载，已热身（1660ms）` |

## 验收清单

```bat
set EXE=%LOCALAPPDATA%\Xifeng-build\dist\Xifeng\Xifeng.exe

:: 1. 不加载模型，验证 boot + 文本规则
%EXE% --text "呃 今天 嗯 开会" --no-inject

:: 2. 真实模型，验证 DLL 收集与模型路径
%EXE% --wav D:\project\workspace\Xifeng\core\fixtures\quality\01.wav --no-inject --print-raw

:: 3. PTT：看第一行必须是 完整性=Medium(0x2000)
%EXE% --ptt --backend mock --no-inject

:: 4. 数据层：库路径 + 历史可查
%EXE% --text "呃 今天 嗯 开会" --no-inject
%EXE% --wav D:\project\workspace\Xifeng\core\fixtures\quality\03.wav --no-inject
%EXE% --history 3
%EXE% --stats
```

第 3 步若打印 `Low(0x1000)`，说明 exe 建在了带 Low 标签的目录里 —— 按上面「构建」一节
换 Medium 解释器与 `%LOCALAPPDATA%` 输出重新打包，别在仓库里改代码。

第 4 步的 `--text` 应打印 `[post] cleaned='今天 开会'`；`--stats` 的 `[db]` 行应是
`%LOCALAPPDATA%\Xifeng\xifeng.db`。

第 4.5 步验证模型纠错（可选；模型放 `%LOCALAPPDATA%\Xifeng\models\llm`，不在包里）：

```bat
%EXE% --text "帮我把 voice i e 的 read me 提交" --correct --no-inject
:: → [correct] applied … [post] cleaned='帮我把 Xifeng 的 README 提交'
%EXE% --text "这个会议机要发给我" --correct --no-inject
:: 守卫拒绝时打印 [correct] 保留原文（…），cleaned 保持原句
```

第 5 步验证托盘 / Web 界面 / 自启（都在 Medium 下跑）：

```bat
:: 5. 托盘：启动后控制台应消失，任务栏通知区出现蓝色麦克风图标
%EXE% --tray
::    右键图标 → 打开主窗口 / 统计 / 模型 / 配置 / 历史 / 关于 / 开机自启 / 退出

:: 6. Web 独立进程：不加载模型，秒开（本地 127.0.0.1:8642 + Edge 应用窗口）
%EXE% --web --tab config

:: 7. 开机自启（写 HKCU，不需要管理员）
%EXE% --autostart on
%EXE% --autostart status
%EXE% --autostart off
```

`--tray` 会先隐藏控制台再加载模型（开机自启不会闪黑窗）；日志在
`%LOCALAPPDATA%\Xifeng\xifeng.log`。重复启动第二个托盘会被命名 Mutex 挡掉。
`--web` 需要 exe 同级的 `frontend/` 目录；关窗 30s 后服务自动退出，
再次点托盘菜单会重新起服务（已有服务时只开窗口）。

## 交付

把整个 `Xifeng` 目录复制到未打标的安装位置（例如
`D:\Program Files (x86)\xifeng`），建快捷方式即可。**不要**放在
`D:\project\workspace\` 下 —— 那里是 Low。

安装目录别和 `%LOCALAPPDATA%\Xifeng` 混用：后者是**用户数据**目录（历史/设置），
升级时如果整体替换安装目录，数据会跟着没。NSIS 默认 perUser 落点正是
`$LOCALAPPDATA\${PRODUCTNAME}`，所以 `src-tauri/nsis/hooks.nsh` 在
`NSIS_HOOK_PREINSTALL` 里把**默认值**改成 `D:\Program Files (x86)\xifeng`
（该目录存在时；否则回退 `%LOCALAPPDATA%\Programs\Xifeng`；用户在目录页手选了
别的路径则尊重其选择）。改完实测：安装目录只有 `xifeng.exe`/`core/`/`uninstall.exe`，
数据目录只有 `xifeng.db`/`models/`/`xifeng.log`。
**静默安装 MUST 把 setup.exe 先复制出仓库**（Low 标签下直接跑会 `exit=2`，与 G38 同源）。

> 重装/覆盖安装会沿用上次安装位置（Tauri 把它记在 `HKCU\Software\xifeng\Xifeng`），
> 此时钩子不会把路径改到新的默认值。要换位置就先卸载、删掉这个键（或安装时手选目录）
> 再装。

### 安装版验收清单（2026-11-18 在 D 盘落点上实跑）

静默安装后按顺序核对，缺一项就不算装好：

| 检查 | 期望 |
|---|---|
| 安装目录 | 只有 `xifeng.exe` / `core/` / `uninstall.exe` |
| 卸载项 | `HKCU\...\Uninstall\Xifeng` 的 `InstallLocation` = 实际目录 |
| 快捷方式 | 指向安装目录的 `xifeng.exe` |
| 用户数据 | 仍在 `%LOCALAPPDATA%\Xifeng`（`xifeng.db` / `models/` / `xifeng.log`），**没被搬** |
| sidecar | `py_call bootstrap` 返回 `version`、模型 `ready:true`；`stats` 能看到旧历史 |
| 更新检查 | `py_call check_update` 返回 `update:false`（仓库 public + Release 在） |
| 热键链路 | 人工按住右 Ctrl 说一句 → `history` 新增 `outcome=Injected` |

热键那一项**只能人工实按**：合成键（`SendInput`）在没有输入桌面的会话里返回成功但
完全无效，详见 G44。实跑一次的真实数字：hold 2351ms、`asr_end` 189ms、注入 7ms、
松手→完成 197ms；另两次分别是空识别（`chars=0`）与纯语气词（后处理拒绝），
都按预期不注入。

### 发版（应用内「检查更新」依赖这一步）

```bash
git tag -a v0.1.0 -m "Xifeng 0.1.0" && git push origin v0.1.0
```

然后在 GitHub 建 Release（tag 选 `v0.1.0`），附两个资产：
`Xifeng_0.1.0_x64-setup.exe` 与 `Xifeng_0.1.0_x64-portable.zip`（`tar -a -c -f` 打便携目录）。

应用内 `check_update` 读 `api.github.com/repos/Topskys/Xifeng/releases/latest`：
- 仓库 MUST 是 **public**（匿名请求，private 一律 404）
- 版本按数字段比较（`0.10.0 > 0.9.0`），无 Release 时提示「仓库暂无 Release」而不是裸 404
- 便携包与安装包的版本号 MUST 与 tag 一致，否则会误报「有新版本」

> 用脚本建 Release 时 MUST 按 UTF-8 发字节：`[Text.Encoding]::UTF8.GetBytes($json)` +
> `Content-Type: application/json; charset=utf-8`。PowerShell 5.1 直接传
> `ConvertTo-Json` 的字符串会把中文写成 `?`（页面成问号，API 已落库，只能 PATCH 重写）。

正式形态的日常使用：安装包把壳装到 `D:\Program Files (x86)\xifeng`（无 D 盘回退
`%LOCALAPPDATA%\Programs\Xifeng`），托盘两项为「显示主界面 / 退出」，
开机自启写 `HKCU\...\Run\Xifeng`（指向壳 exe）。
以下「已知限制」表里带 `--tray` / 控制台隐藏的行属于 PyInstaller 形态。

## 已知限制

| 限制 | 影响 | 计划 |
|---|---|---|
| 控制台程序 + 运行期隐藏 | 启动瞬间可能闪一下窗口（pystray 之前） | 可改 `--windowed` 双份产物，但体积翻倍，暂不做 |
| 模型外置 | 复制目录时容易漏 | `--with-models` 或后续安装包 |
| 无代码签名 | SmartScreen 首次拦截 | 后续 |
| 每次启动加载模型 ~2.6s | 托盘常驻后只付一次 | 已由 `--tray` + 开机自启覆盖 |
| `auto` 只兜底可读控件 | 控制台/游戏/自绘控件读不到文本，不自动兜底（防重复注入） | 用 `--inject clipboard` 显式指定 |
| 剪贴板兜底会短暂占用剪贴板 | 约 500ms 内其他程序的复制会丢 | `--clipboard-restore-ms` 可调；检测到被改写会跳过恢复 |
