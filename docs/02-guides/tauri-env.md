# Tauri 开发环境（本机实测）

> 状态：**可用**（2026-11-09）。WebView2 / MSVC / rustc / cargo / Tauri CLI 全部 ✔。
> 唯一坑：Rust 装在 `D:\env\rust`，**不在默认 PATH**，每次开新终端都要先设环境。

## 一次性设置（当前 PowerShell 会话）

```powershell
$env:PATH = "D:\env\rust\.cargo\bin;$env:PATH"
$env:RUSTUP_HOME = "D:\env\rust\.rustup"
$env:CARGO_HOME  = "D:\env\rust\.cargo"
# link.exe 在本机写 %TEMP% 会被拒（G35），必须指到仓库内目录
$env:TEMP = "D:\project\workspace\voice-ime\.rust-temp"
$env:TMP  = $env:TEMP
```

`tools/dev-env.ps1` 就是这段，用 dot-source 引入：

```powershell
. D:\project\workspace\voice-ime\spike\tools\dev-env.ps1
```

## 验证

```powershell
cd spike\frontend
pnpm tauri info
```

应看到：

```
✔ WebView2: 157.0.4341.42
✔ MSVC: Visual Studio 生成工具 2022
✔ rustc: 1.99.1
✔ cargo: 1.99.1
✔ rustup: 1.29.3
✔ Rust toolchain: stable-x86_64-pc-windows-msvc
```

`tauri info` 若报 `rustc: not installed`，99% 是没设 PATH —— 不是真没装。

## 开发 / 打包

```powershell
cd spike\frontend
pnpm tauri:dev      # 开窗口 + 拉起 Python 子进程（热重载）
pnpm tauri:build    # 出 NSIS 安装包
```

CLI 版本：`@tauri-apps/cli 2.12.4`（**pnpm 装，不要用 npm**，仓库已有 `pnpm-lock.yaml`）。

## 已知依赖

| 组件 | 本机状态 | 说明 |
|---|---|---|
| WebView2 运行时 | 157.0.4341.42 | Win10/11 一般自带；Win10 老版本需装 Evergreen |
| MSVC 生成工具 | VS 2022 BuildTools | 已在 `D:\env\vs\BuildTools` |
| NSIS | **v3.11 已装**（便携版 `D:\env\NSIS`） | `tauri build` 打安装包用；`tools\dev-env.cmd` 会把它加进 PATH |
| Rust toolchain | 1.99.1 stable-msvc | `rust-toolchain.toml` 已锁定 |

NSIS 不是 Node 包，**装不了** `pnpm add`。它是原生 `makensis.exe`，三条路：

1. **已装，直接用**（本机现状）：便携版在 `D:\env\NSIS`，`Bin\makensis.exe` + 同目录
   `zlib1.dll`。`tools\dev-env.cmd` 会自动把它加进 PATH，所以 `pnpm tauri:build` 能过。
2. `winget install NSIS.NSIS`（本机实测卡住无响应，不推荐）
3. 让 Tauri 自己下载：去掉 `--no-bundle` 跑一次 `tauri build`，CLI 会拉到
   `%LOCALAPPDATA%\tauri\NSIS`。

⚠ `makensis.exe` **不能单独拷出来**：它依赖同目录的 `zlib1.dll`，拷到别处直接
`0xC0000135`。所以别复制单文件，把整个 `Bin` 目录放进 PATH。

## 与 PyInstaller 的关系

两条线并存，不冲突：

- **当前可交付**：`packaging/build.py`（PyInstaller onedir，145MB）
- **迁移中**：Tauri 壳 + Python 子进程（`src-tauri/`）

Tauri 路线完整后，PyInstaller 可只作为"Python 核心的便携打包器"保留，或整体退役。
迁移方案见 `docs/00-process/plan.md` 的 D14。

## 大坑：WebView2 创建失败 `HRESULT 0x800700AA`

**症状**：exe 一启动就 panic 退出：

```
Failed to setup app: runtime error: failed to create webview:
WebView2 error: WindowsError(Error { code: HRESULT(0x800700AA), message: "请求的资源在使用中。" })
```

**根因不是"资源被占用"，而是没有可写的用户数据目录**。本机沙箱进程对
`%LOCALAPPDATA%`、`%APPDATA%`、`%TEMP%` **全部无写权限**（实测三个目录写文件都
`PermissionError`），WebView2 无法在那里建默认 profile，于是报 ERROR_BUSY。

**解法**：把 WebView2 的用户数据目录指到可写位置（`tools/dev-env.cmd` 已内置）：

```powershell
$env:WEBVIEW2_USER_DATA_FOLDER = "D:\project\workspace\voice-ime\.rust-temp\wv2"
```

设了之后窗口正常打开（标题 `VoiceIME`），Python 子进程也随之启动。

**验证成功的完整命令**：

```powershell
cd spike
tools\dev-env.cmd                      # 已含 WEBVIEW2_USER_DATA_FOLDER
cd frontend\src-tauri
$env:VOICEIME_PYTHON = "D:\project\workspace\voice-ime\spike\.venv\Scripts\python.exe"
.\target\release\voiceime-tauri.exe
```

判据：窗口标题出现 `VoiceIME`，且 `Get-Process python` 能看到子进程。

## 目录布局（重要）

`src-tauri/` MUST 在**运行 tauri 命令的目录**下。本项目放在 `spike/frontend/src-tauri`，
因此命令要从 `spike/frontend` 执行：

```powershell
cd spike\frontend
pnpm tauri:dev
pnpm tauri:build
```

放在 `spike/src-tauri` 会报
`Couldn't recognize the current folder as a Tauri project`。
同时根 `Cargo.toml` 的 `workspace.exclude` 要含 `spike/frontend/src-tauri`，
否则 `cargo metadata` 会报 "current package believes it's in a workspace when it's not"。