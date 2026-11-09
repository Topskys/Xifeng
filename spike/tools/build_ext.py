"""编译 Rust 扩展并装进当前 venv。

为什么不直接用 `maturin develop`：本机（沙箱环境）里 maturin 的 `tempfile` 建临时目录
会被拒（`os error 5`），而 `cargo build` 本身正常。这里做的是同一件事——
`cargo build` 出 `cdylib`，再把 `voiceime_rs.dll` 改名成 `.pyd` 放到 site-packages。
maturin 仍留在 dev 依赖里，供无此限制的机器 / CI 打包 wheel。

用法：
    uv run python tools/build_ext.py            # debug
    uv run python tools/build_ext.py --release  # 更快，体积更小
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import sysconfig

ROOT = pathlib.Path(__file__).resolve().parents[2]  # 仓库根目录
MANIFEST = ROOT / "crates" / "ime-py" / "Cargo.toml"
MODULE = "voiceime_rs"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true", help="用 release profile 构建")
    args = ap.parse_args()
    profile = "release" if args.release else "debug"

    cmd = ["cargo", "build", "-p", "ime-py", "--manifest-path", str(MANIFEST)]
    if args.release:
        cmd.append("--release")
    print("+", " ".join(cmd))
    if subprocess.run(cmd, cwd=ROOT).returncode != 0:
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
