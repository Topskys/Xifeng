import json
import sys
import urllib.request

sys.path.insert(0, "tools")
from cdp_probe import _recv, _send, _ws_connect  # noqa: E402

JS = """
(async () => {
  try {
    const v = await window.__TAURI_INTERNALS__.invoke("py_call",
        { method: "bootstrap", params: {} });
    return "OK " + JSON.stringify(v).slice(0, 120);
  } catch (e) {
    return "ERR: " + String(e);
  }
})()
"""


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "9334"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=8) as r:
        page = [x for x in json.load(r) if x.get("type") == "page"][0]
    sock = _ws_connect(page["webSocketDebuggerUrl"])
    try:
        _send(sock, json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": JS, "awaitPromise": True,
                       "returnByValue": True},
        }))
        while True:
            m = json.loads(_recv(sock))
            if m.get("id") == 1:
                break
        print(m["result"]["result"].get("value"))
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
