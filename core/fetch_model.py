"""下载 SenseVoice int8 模型（sherpa-onnx 格式）。

    uv run python fetch_model.py                       # 默认 hf-mirror
    uv run python fetch_model.py --mirror hf           # 官方源（国内常不通）
    uv run python fetch_model.py --dir ../models/asr/sensevoice-small-int8

只下载两个文件：`model.int8.onnx`（约 228MiB）与 `tokens.txt`。
支持断点续传：中断后重跑会从已有字节继续，不重下。

为什么用 curl 而不是 urllib：
hf-mirror.com 的证书链在本机校验失败（`CERTIFICATE_VERIFY_FAILED: certificate has expired`），
而 Windows 自带的 curl 走 schannel 正常。宁可换下载器，也不关证书校验。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
FILES = ["model.int8.onnx", "tokens.txt"]

MIRRORS = {
    "hf-mirror": f"https://hf-mirror.com/{REPO}/resolve/main",
    "hf": f"https://huggingface.co/{REPO}/resolve/main",
    # ModelScope 官方源：国内直连，无需经 hf-mirror 代理。
    # 若 repo/owner 实际与 HF 不同，请在此处按 ModelScope 上的页面地址修正。
    "modelscope": f"https://modelscope.cn/models/{REPO}/resolve/master",
}

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "models" / "asr" / "sensevoice-small-int8"


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GiB"


def download_curl(url: str, dest: Path, expect: int = 0) -> None:
    have = dest.stat().st_size if dest.exists() else 0
    if expect and have == expect:
        print(f"[skip] {dest.name} 已完整（{human(have)}）")
        return
    if expect and have > expect:
        print(f"[warn] {dest.name} 比远端大，删除重下")
        dest.unlink()
        have = 0

    cmd = [
        "curl", "-L", "--fail", "-sS",
        "--retry", "10", "--retry-delay", "2", "--retry-all-errors",
        "--speed-limit", "2048", "--speed-time", "30",   # 卡死 30s 就断开重试
        "-o", str(dest),
    ]
    if have:
        print(f"[resume] {dest.name} 从 {human(have)} 继续")
        cmd += ["-C", "-"]
    cmd.append(url)
    print(f"[get] {dest.name} ({human(expect) if expect else '大小未知'})")
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"curl 失败（exit {rc}）：{url}")

    size = dest.stat().st_size
    if expect and size != expect:
        raise SystemExit(f"大小不符：{size} != {expect}")


def download_urllib(url: str, dest: Path, expect: int = 0) -> None:
    """无 curl 时的退路；证书问题时需自行处理 CA。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0
    if expect and have == expect:
        print(f"[skip] {dest.name} 已完整")
        return
    headers = {"Range": f"bytes={have}-"} if have else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("ab") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    if expect and dest.stat().st_size != expect:
        raise SystemExit("大小不符")


def remote_size(url: str) -> int:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(r.headers.get("Content-Length", 0))
    except (urllib.error.URLError, TimeoutError):
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="下载 SenseVoice int8（sherpa-onnx）")
    p.add_argument("--mirror", default="hf-mirror", choices=sorted(MIRRORS))
    p.add_argument("--dir", default=str(DEFAULT_DIR))
    p.add_argument("--force", action="store_true", help="忽略已存在的文件")
    args = p.parse_args(argv)

    base = MIRRORS[args.mirror]
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    print(f"[fetch] {base}\n[dst]   {out}")

    has_curl = shutil.which("curl") is not None
    if not has_curl:
        print("[warn] 未找到 curl，退回 urllib（可能因证书失败）")

    for name in FILES:
        url = f"{base}/{name}"
        dest = out / name
        if args.force and dest.exists():
            dest.unlink()
        expect = remote_size(url)
        if has_curl:
            download_curl(url, dest, expect)
        else:
            download_urllib(url, dest, expect)

    print(f"[done] {out}")
    print("       uv run python run_spike.py --seconds 3 --backend sherpa --no-inject --print-raw")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
