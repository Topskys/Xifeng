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
| NSIS | **未装** | `tauri build` 打安装包时才需要；开发用 `tauri:dev` 不需要 |
| Rust toolchain | 1.99.1 stable-msvc | `rust-toolchain.toml` 已锁定 |

需要 NSIS 时：`winget install NSIS.NSIS`，或按 `docs/02-guides/rust-env.md` 的"坑"一节手工解压到 `D:\env\NSIS`。

## 与 PyInstaller 的关系

两条线并存，不冲突：

- **当前可交付**：`packaging/build.py`（PyInstaller onedir，145MB）
- **迁移中**：Tauri 壳 + Python 子进程（`src-tauri/`）

Tauri 路线完整后，PyInstaller 可只作为"Python 核心的便携打包器"保留，或整体退役。
迁移方案见 `docs/00-process/plan.md` 的 D14。
