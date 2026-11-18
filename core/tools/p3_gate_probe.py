"""P2-③ 手动验证：voice_test 门控（release 无 e2e → 显式报错；e2e 壳 → OK）。

    uv run python -X utf8 tools/p3_gate_probe.py 9334
"""

from __future__ import annotations

import json
import sys
import urllib.request

sys.path.insert(0, "tools")
from cdp_probe import _recv, _send, _ws_connect  # noqa: E402


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "9334"
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=8))
    try:
        page = next(p for p in pages if p.get("url") == "http://tauri.localhost/")
    except StopIteration:
        print("main 窗口未找到")
        return 1
    sock = _ws_connect(page["webSocketDebuggerUrl"])
    js = (
        "(async () => { try {"
        " await window.__TAURI_INTERNALS__.invoke('voice_test', { mode: 'tap' });"
        " return 'UNEXPECTED-OK';"
        " } catch (e) { return 'ERR: ' + String(e); } })()"
    )
    _send(sock, json.dumps({
        "id": 1, "method": "Runtime.evaluate",
        "params": {"expression": js, "awaitPromise": True, "returnByValue": True},
    }))
    while True:
        m = json.loads(_recv(sock))
        if m.get("id") == 1:
            break
    print(json.dumps(m["result"]["result"], ensure_ascii=False))
    sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
