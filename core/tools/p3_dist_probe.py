"""P2-③ 手动验证：新安装形态冒烟（bootstrap.correction + correct 方法存在性）。

    uv run python -X utf8 tools/p3_dist_probe.py 9334
"""

from __future__ import annotations

import json
import sys
import urllib.request

sys.path.insert(0, "tools")
from cdp_probe import _recv, _send, _ws_connect  # noqa: E402


def py_call(sock: dict, rid: int, method: str, params: dict) -> dict:
    js = (
        "(async () => { try { const r = await window.__TAURI_INTERNALS__.invoke("
        "'py_call', { method: " + json.dumps(method) + ", params: "
        + json.dumps(params) + " }); return JSON.stringify(r); }"
        " catch (e) { return JSON.stringify({ __err: String(e) }); } })()"
    )
    _send(sock, json.dumps({
        "id": rid, "method": "Runtime.evaluate",
        "params": {"expression": js, "awaitPromise": True, "returnByValue": True},
    }))
    while True:
        m = json.loads(_recv(sock))
        if m.get("id") == rid:
            break
    return json.loads(m["result"]["result"]["value"])


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "9334"
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=8))
    try:
        page = next(p for p in pages if p.get("url") == "http://tauri.localhost/")
    except StopIteration:
        print("main 窗口未找到")
        return 1
    sock = _ws_connect(page["webSocketDebuggerUrl"])
    boot = py_call(sock, 1, "bootstrap", {})
    corr = boot.get("correction", {})
    print("bootstrap.correction:", json.dumps(corr, ensure_ascii=False))
    # correct 方法存在性：不带模型也应返回 applied=false 而不是「未知方法」
    r = py_call(sock, 2, "correct", {"text": "冒烟测试文本"})
    print("correct →", json.dumps(r, ensure_ascii=False)[:200])
    sock.close()
    ok = isinstance(corr, dict) and "applied" in r
    print("结论：", "新 core（含 correct）✓" if ok else "✗ core 方法缺失")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
