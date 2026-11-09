# Rust 工具链环境（M2 起）

> 状态：**已装好**（2026-11-09）。全部落在 `D:` 盘，仓库目录零改动。
> Rust 版本身仍是推迟项（`00-process/plan.md` D6），这里只解决「怎么装才不多下载、不污染 C 盘」。

## 装了什么

| 用途 | 版本 | 位置 | 体积 |
|---|---|---|---|
| rustup | 1.29.3 | `D:\env\rust\.rustup`（`RUSTUP_HOME`） | — |
| rustc / cargo | 1.99.1 | `D:\env\rust\.cargo`（`CARGO_HOME`）、落盘在 `.rustup\toolchains` | 603 MB |
| MSVC 编译器 + 链接器 | 14.44.35207（VS Build Tools 2022 17.14.44） | `D:\env\vs\BuildTools` | 1 671 MB |
| Windows SDK | 10.0.22621.0 | `D:\Windows Kits\10` | 1 904 MB |
| NSIS（`makensis`） | 3.11 | `D:\env\NSIS\Bin`（在 `PATH`） | 2 MB |
| VS 安装包缓存 | — | `D:\env\vs\cache`（可随时删，删了只能重下） | 630 MB |

合计约 **4.8 GB**；C 盘只多了 VS 安装器引擎本体（~160 MB）。

工具链是 **最小集**：`rustup --profile minimal`（只有 `stable-x86_64-pc-windows-msvc` 一个
toolchain、一个 target，没有 docs/clippy/rustfmt）；VS 侧只装两个组件，`includeRecommended=0`。

## 装法（复现用）

脚本：`D:\env\setup-rust.ps1`。要点都踩过坑，照抄即可：

```powershell
# 1) rustup —— static.rust-lang.org 在本机基本连不上，必须换镜像
$env:RUSTUP_DIST_SERVER = 'https://mirrors.tuna.tsinghua.edu.cn/rustup'
$env:RUSTUP_UPDATE_ROOT = 'https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup'
$env:RUSTUP_HOME = 'D:\env\rust\.rustup'
$env:CARGO_HOME  = 'D:\env\rust\.cargo'
.\rustup-init.exe -y --profile minimal --default-toolchain stable-x86_64-pc-windows-msvc --no-modify-path

# 2) VS Build Tools —— 只加两个组件，不装 workload、不加 --includeRecommended
#    共享目录/缓存必须在第一次安装前写注册表，否则事后改会留悬空路径
#    HKLM\SOFTWARE\Microsoft\VisualStudio\Setup  ->  SharedInstallationPath, CachePath
winget install --id Microsoft.VisualStudio.2022.BuildTools -e `
  --accept-package-agreements --accept-source-agreements --disable-interactivity `
  --override "--quiet --wait --installPath D:\env\vs\BuildTools --cache `
             --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
             --add Microsoft.VisualStudio.Component.Windows11SDK.22621"
```

NSIS 取官方 zip（2.25 MB）解到 `D:\env\NSIS`，删掉 `Docs/Examples/Contrib/Menu` 与
`makensisw.exe`/`NSIS.chm` 后只剩 2 MB。

## 验证

新开一个终端（`setx` 写的是用户作用域，老窗口吃不到）：

```powershell
rustc --version        # rustc 1.99.1
cargo --version        # cargo 1.99.1
rustup show            # stable-x86_64-pc-windows-msvc (active, default)
makensis /VERSION      # v3.11
cargo new D:\temp\smoke; cd D:\temp\smoke; cargo build   # Finished dev profile in ~4s
```

`cargo build` 能过，说明 `link.exe` 与 SDK 的 `kernel32.lib` 都已被 rustc 自动找到
（rustc 走注册表 `HKLM\SOFTWARE\WOW6432Node\Microsoft\Microsoft SDKs\Windows\v10.0`
的 `InstallationFolder`，不需要 `vcvars`、不需要设 `LIB`/`INCLUDE`）。

## 坑

- **`static.rust-lang.org` 拉不动**：`Invoke-WebRequest` 会挂在 0 字节。用 TUNA 镜像，
  并把 `RUSTUP_DIST_SERVER` 一并指过去，否则 toolchain 还是回源。
- **SourceForge 直链拿不到 ZIP**：`downloads.sourceforge.net/.../nsis-3.11.zip` 无论走哪个
  镜像域名，回来的都是 125 KB 的 HTML 跳转页（带 `meta refresh` + 签名 `ts` token）。
  必须先取页面、正则抠 `content="5; url=..."` 里那个带 token 的地址，再请求第二次才是真 ZIP。
- **`makensis.exe` 不能单独拷出来**：它依赖同目录的 `zlib1.dll`，拷到别处直接
  `0xC0000135`。所以不要造 `D:\env\NSIS\makensis.exe` 这个副本，把 `D:\env\NSIS\Bin` 放进 `PATH`。
- **SDK 没进 `SharedInstallationPath`**：写了 `SharedInstallationPath=D:\env\vs\Shared` 之后，
  SDK 仍落在 **`D:\Windows Kits\10`**（VS 安装器把 SDK 放在「VS 所在盘」的固定目录，
  这个键只对部分共享组件生效）。仍在 D 盘，不动它——搬走会让注册表里的路径全部悬空。
- **别多开安装器**：VS 安装器有全局互斥，第二个 `winget install` 会排队/报错；
  安装期间不要再触发一次 UAC 脚本。

## 还没装（用到再说）

| 缺的东西 | 什么时候需要 |
|---|---|
| `cmake` / `ninja` | `llama-cpp-2`（纠错推理）用 `cmake` crate 编 C++，届时 `winget install Kitware.CMake` |
| `just` | 计划里的 `justfile`；纯 Rust 阶段先用 `cargo` 原生命令即可 |
| `sherpa-rs` 的 onnxruntime 动态库 | Step 5 接 ASR 时按平台带预编译 DLL，不走 cmake |
