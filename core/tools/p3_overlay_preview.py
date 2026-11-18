"""浮窗样式预览：CDP 把 overlay 页导航到 demo=1（合成说话电平），抓多帧。

    uv run python -X utf8 tools/p3_overlay_preview.py 9334

无需真实麦克风/会话 —— demo 参数直接驱动音柱。输出三帧到 .tauri-work/。
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "tools")
from cdp_probe import _recv, _send, _ws_connect  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / ".cache" / "tauri-work"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "9334"
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=8))
    overlay = next(p for p in pages if "overlay" in str(p.get("url", "")))
    sock = _ws_connect(overlay["webSocketDebuggerUrl"])
    try:
        _send(sock, json.dumps({"id": 1, "method": "Page.navigate", "params": {
            "url": "http://tauri.localhost/index.html?window=overlay&demo=1"}}))
        while True:
            m = json.loads(_recv(sock))
            if m.get("id") == 1:
                break
        time.sleep(0.6)   # 页面加载 + 动效跑起来
        for i, delay in enumerate((0.0, 0.9, 1.8)):
            if delay:
                time.sleep(delay)
            _send(sock, json.dumps({"id": 10 + i, "method": "Page.captureScreenshot",
                                    "params": {"format": "png"}}))
            while True:
                m = json.loads(_recv(sock))
                if m.get("id") == 10 + i:
                    break
            out = rf"{OUT_DIR}\overlay-demo-{i}.png"
            with open(out, "wb") as f:
                f.write(base64.b64decode(m["result"]["data"]))
            print("已存", out)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
