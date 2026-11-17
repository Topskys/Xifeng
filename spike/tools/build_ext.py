"""编译 Rust 扩展并装进当前 venv。

为什么不直接用 `maturin develop`：本机（沙箱环境）里 maturin 的 `tempfile` 建临时目录
会被拒（`os error 5`），而 `cargo build` 本身正常。这里做的是同一件事——
`cargo build` 出 `cdylib`，再把 `xifeng_rs.dll` 改名成 `.pyd` 放到 site-packages。
maturin 仍留在 dev 依赖里，供无此限制的机器 / CI 打包 wheel。

用法：
    uv run python tools/build_ext.py            # debug
    uv run python tools/build_ext.py --release  # 更快，体积更小
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import sysconfig

ROOT = pathlib.Path(__file__).resolve().parents[2]  # 仓库根目录
MANIFEST = ROOT / "crates" / "ime-py" / "Cargo.toml"
MODULE = "xifeng_rs"

# cargo 不一定在 PATH 里（本机装在 D:\env\rust，见 docs/02-guides/rust-env.md），
# 所以显式找几个常见位置，避免"明明装了却报 WinError 2"。
CARGO_CANDIDATES = (
    pathlib.Path(r"D:\env\rust\.cargo\bin\cargo.exe"),
    pathlib.Path.home() / ".cargo" / "bin" / "cargo.exe",
)
# G35：本机进程对默认 %TEMP% 无完整写权限，link.exe 会 LNK1104 打不开临时文件
TEMP_FALLBACK = pathlib.Path(r"D:\env\tmp")
# rustup/cargo 的 HOME 也装在 D 盘（C 盘那份不可写，会报 os error 5）
RUSTUP_HOME = pathlib.Path(r"D:\env\rust\.rustup")
CARGO_HOME = pathlib.Path(r"D:\env\rust\.cargo")


def find_cargo() -> str | None:
    found = shutil.which("cargo")
    if found:
        return found
    for c in CARGO_CANDIDATES:
        if c.exists():
            return str(c)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true", help="用 release profile 构建")
    args = ap.parse_args()
    profile = "release" if args.release else "debug"

    cargo = find_cargo()
    if cargo is None:
        print(
            "找不到 cargo。装法见 docs/02-guides/rust-env.md，"
            f"或把 cargo 加进 PATH（试过：{', '.join(str(c) for c in CARGO_CANDIDATES)}）",
            file=sys.stderr,
        )
        return 1

    env = dict(os.environ)
    # TEMP 优先级：调用方已设 → 仓库内 .rust-temp → D:\env\tmp。
    # 本机相对这两处写普通文件都行，但 link.exe 要建带显式安全描述符的临时文件，
    # 只有仓库内目录实测能过（G35）。
    if "TEMP" not in os.environ:
        repo_tmp = ROOT / ".rust-temp"
        if repo_tmp.exists():
            env["TEMP"] = env["TMP"] = str(repo_tmp)
        elif TEMP_FALLBACK.exists():
            env["TEMP"] = env["TMP"] = str(TEMP_FALLBACK)
    # 只在默认位置不可用时才覆盖，避免干扰正常装机
    if RUSTUP_HOME.exists() and "RUSTUP_HOME" not in env:
        env["RUSTUP_HOME"] = str(RUSTUP_HOME)
    if CARGO_HOME.exists() and "CARGO_HOME" not in env:
        env["CARGO_HOME"] = str(CARGO_HOME)

    cmd = [cargo, "build", "-p", "ime-py", "--manifest-path", str(MANIFEST)]
    if args.release:
        cmd.append("--release")
    print("+", " ".join(cmd))
    if subprocess.run(cmd, cwd=ROOT, env=env).returncode != 0:
        return 1

    built = ROOT / "target" / profile / f"{MODULE}.dll"
    if not built.exists():
        print(f"找不到构建产物：{built}", file=sys.stderr)
        return 1

    dest = pathlib.Path(sysconfig.get_paths()["purelib"]) / f"{MODULE}.pyd"
    shutil.copy2(built, dest)
    print(f"installed -> {dest}  ({dest.stat().st_size / 1024:.0f} KB, {profile})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
