r"""用 CDP Page.captureScreenshot 抓 Web GUI 各页（比 PrintWindow 可靠）。

    python cdp_shot.py --out ../temp/web-shots
MUST 用 Medium 解释器跑。要求 Edge 以 --remote-debugging-port=9223 +
独立 --user-data-dir 启动（新版 Edge 禁止默认 profile 开调试端口）。
"""

from __future__ import annotations

import argparse
import base64
import time
from pathlib import Path

from cdp_ask import WS, http_json

PAGES = ["stats", "models", "config", "history", "about"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="")
    args = p.parse_args()

    out = Path(args.out) if args.out else \
        Path(__file__).resolve().parents[1] / "temp" / "web-shots"
    out.mkdir(parents=True, exist_ok=True)

    tabs = http_json("http://127.0.0.1:9223/json/list")
    pages = [t for t in tabs if t.get("type") == "page"]
    # 有多个 page（Edge 首次启动可能弹同步/欢迎页）时只抓应用本身
    page = next((t for t in pages if t.get("url", "").startswith("http://127.0.0.1")), None)
    if page is None:
        raise RuntimeError("没找到 Web GUI 页面，只有：" + ", ".join(t.get("url", "") for t in pages))
    ws = WS(page["webSocketDebuggerUrl"])

    # 统一视口（CSS 像素，dpr=1 → 截图即设计尺寸）
    ws.call("Emulation.setDeviceMetricsOverride",
            {"width": 1180, "height": 780, "deviceScaleFactor": 1, "mobile": False})
    ws.call("Page.enable", {})
    # 重新加载：确保截到的是最新 dist（改了前端不重建就会看到旧界面，G31D）
    ws.call("Page.reload", {"ignoreCache": True})
    time.sleep(2.0)

    def evaluate(expr: str) -> None:
        ws.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True})

    def shot(name: str) -> None:
        time.sleep(0.6)
        for _ in range(3):
            res = ws.call("Page.captureScreenshot", {"format": "png"})
            if "data" in res.get("result", {}):
                (out / name).write_bytes(base64.b64decode(res["result"]["data"]))
                print(f"[shot] {out / name}")
                return
            print(f"[retry] {name}: {json.dumps(res)[:120]}")
            time.sleep(1)
        raise RuntimeError(f"截图失败：{name}")

    for theme in ("dark", "light"):
        evaluate(f"document.documentElement.classList.toggle('dark', theme === 'dark')"
                 .replace("theme", f"'{theme}'"))
        time.sleep(0.4)
        for pg in PAGES:
            evaluate(f"location.hash = '{pg}'")
            shot(f"{theme}-{pg}.png")

    # 恢复默认深色（class 由 setTheme 流程管理，这里只还原视觉）
    evaluate("document.documentElement.classList.add('dark')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
