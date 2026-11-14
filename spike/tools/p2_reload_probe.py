"""P2-① 手动验证：voice_reload_settings 热更命令经 CDP 驱动。

    uv run python -X utf8 tools/p2_reload_probe.py 9334

调用两次（并发安全冒烟），壳 stderr 应出现「设置已热更」。
"""

from __future__ import annotations

import json
import sys
import urllib.request

sys.path.insert(0, "tools")
from cdp_probe import _recv, _send, _ws_connect  # noqa: E402


def invoke(port: str, cmd: str) -> str:
    # 纯拼接，不用 f-string——花括号折叠规则容易把 JS 括号搞坏
    js = (
        '(async () => { try { const r = await window.__TAURI_INTERNALS__.invoke('
        + json.dumps(cmd)
        + '); return "OK " + JSON.stringify(r); } '
        'catch (e) { return "ERR: " + String(e); } })()'
    )
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=8))
    # 选 main 窗口（overlay 页先创建、排第一，但命令验证要在主窗口做）
    try:
        page = next(p for p in pages if p.get("url") == "http://tauri.localhost/")
    except StopIteration:
        return "ERR: main 窗口未找到"
    sock = _ws_connect(page["webSocketDebuggerUrl"])
    try:
        _send(sock, json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": js, "awaitPromise": True, "returnByValue": True},
        }))
        while True:
            m = json.loads(_recv(sock))
            if m.get("id") == 1:
                break
        # 诊断工具：打完整响应，别只摘 value（排查 CDP 层问题靠它）
        return json.dumps(m["result"], ensure_ascii=False)[:300]
    finally:
        sock.close()


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "9334"
    print("reload #1:", invoke(port, "voice_reload_settings"))
    print("reload #2:", invoke(port, "voice_reload_settings"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
