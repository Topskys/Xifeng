"""浮窗截图：录音会话期间经 CDP 抓 overlay 页（验证新音柱动效）。

    uv run python -X utf8 tools/p3_overlay_shot.py 9334 <输出.png>

流程：先发 down（会话开始，浮窗出现）→ 等 0.8s（喂几批音频）→
对 overlay 页 Page.captureScreenshot → 存文件（不 up，调用方自己 up）。
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request

sys.path.insert(0, "tools")
from cdp_probe import _recv, _send, _ws_connect  # noqa: E402
from voice_e2e import voice_test  # noqa: E402


def shoot(pages: list[dict], out: str) -> None:
    overlay = next(p for p in pages if "overlay" in str(p.get("url", "")))
    sock = _ws_connect(overlay["webSocketDebuggerUrl"])
    try:
        _send(sock, json.dumps({"id": 1, "method": "Page.captureScreenshot",
                                "params": {"format": "png"}}))
        while True:
            m = json.loads(_recv(sock))
            if m.get("id") == 1:
                break
        data = base64.b64decode(m["result"]["data"])
        with open(out, "wb") as f:
            f.write(data)
        print(f"已存 {out}（{len(data)} 字节）")
    finally:
        sock.close()


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "9334"
    out = sys.argv[2] if len(sys.argv) > 2 else r"D:\project\workspace\voice-ime\.tauri-work\overlay.png"
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=8))
    try:
        print("down:", voice_test(port, "down"))
        time.sleep(0.9)   # 等浮窗显示 + 喂送几批（音量条有数据）
        shoot(pages, out)
    finally:
        print("up:", voice_test(port, "up"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
