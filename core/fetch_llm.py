"""下载 L1 纠错所需资源：Qwen2.5-0.5B GGUF + llama.cpp CPU 版 llama-server。

    uv run python fetch_llm.py                  # 默认 hf-mirror + GitHub
    uv run python fetch_llm.py --mirror hf

落位：models/llm/qwen2.5-0.5b-instruct-q4_k_m.gguf + models/llm/bin/llama-server.exe
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_model import human  # noqa: E402  复用 human() 打印

OUT = Path(__file__).resolve().parent.parent / "models" / "llm"
GGUF_NAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
GGUF_URLS = [
    f"https://hf-mirror.com/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/{GGUF_NAME}",
    f"https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/{GGUF_NAME}",
]


def curl(url: str, dest: Path, flags: list[str] | None = None) -> None:
    cmd = [r"C:\Windows\System32\curl.exe", "-L", "--fail", "-sS",
           "--ssl-no-revoke", "--retry", "5", * (flags or []), "-o", str(dest), url]
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"curl 失败（exit {rc}）：{url}")


def latest_llamacpp_asset() -> str:
    """从 GitHub latest release 找 llama-server 的 CPU (avx2) Windows 包。"""
    url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "xifeng"}), timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = data["tag"]
    pat = re.compile(r"llama-.*-bin-win-cpu-x64\.zip$", re.I)
    for a in data["assets"]:
        if pat.search(a["name"]):
            return tag, a["browser_download_url"]
    raise SystemExit(f"latest release {tag} 里没有 cpu-x64 包")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-llamacpp", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    gguf = OUT / GGUF_NAME
    if gguf.exists() and gguf.stat().st_size > 100_000_000:
        print(f"[skip] {gguf.name} 已存在（{human(gguf.stat().st_size)}）")
    else:
        print(f"[get] {GGUF_NAME}")
        for url in GGUF_URLS:
            try:
                curl(url, gguf, ["--retry-delay", "2"])
                break
            except SystemExit:
                print("[warn] 源失败，换下一个")
        print(f"[done] {human(gguf.stat().st_size)}")

    if not args.skip_llamacpp:
        bin_dir = OUT / "bin"
        bin_dir.mkdir(exist_ok=True)
        if not (bin_dir / "llama-server.exe").exists():
            tag, asset = latest_llamacpp_asset()
            print(f"[get] llama.cpp {tag} (cpu-x64)")
            z = bin_dir / "llamacpp.zip"
            curl(asset, z, ["--retry-delay", "2"])
            import zipfile

            with zipfile.ZipFile(z) as zf:
                for name in zf.namelist():
                    if name.endswith((".exe", ".dll")):
                        zf.extract(name, bin_dir)
            z.unlink()
            # 二进制可能嵌在子目录里，统一挪到 bin/ 根
            for exe in bin_dir.rglob("*.exe"):
                if exe.parent != bin_dir:
                    exe.replace(bin_dir / exe.name)
            for dll in bin_dir.rglob("*.dll"):
                if dll.parent != bin_dir:
                    dll.replace(bin_dir / dll.name)
        print(f"[done] {bin_dir / 'llama-server.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
