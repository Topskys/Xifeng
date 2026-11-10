import json
import sys
import urllib.request

sys.path.insert(0, "tools")
from cdp_probe import _recv, _send, _ws_connect  # noqa: E402


def main() -> int:
    with urllib.request.urlopen("http://127.0.0.1:9333/json", timeout=8) as r:
        page = [x for x in json.load(r) if x.get("type") == "page"][0]
    sock = _ws_connect(page["webSocketDebuggerUrl"])
    counter = [0]

    def ev(expr: str, await_p: bool = False):
        counter[0] += 1
        _send(sock, json.dumps({
            "id": counter[0], "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_p},
        }))
        while True:
            m = json.loads(_recv(sock))
            if m.get("id") == counter[0]:
                return m.get("result", {})

    try:
        print("--- 标题栏结构 ---")
        r = ev("""(() => ({
            header: !!document.querySelector('header'),
            buttons: [...document.querySelectorAll('header button')].map(b => b.getAttribute('aria-label')),
            disabled: [...document.querySelectorAll('header button')].map(b => b.disabled),
        }))()""")
        print(" ", json.dumps(r["result"]["value"], ensure_ascii=False))

        print("--- 窗口命令 ---")
        for cmd in ("is_maximized",):
            r = ev(
                f'window.__TAURI_INTERNALS__.invoke("{cmd}")'
                '.then(v => "ok:" + String(v)).catch(e => "ERR:" + e)',
                await_p=True,
            )
            print(f"  {cmd} -> {r.get('result', {}).get('value')}")

        print("--- 最大化切换实测 ---")
        r = ev(
            'window.__TAURI_INTERNALS__.invoke("toggle_maximize")'
            '.then(v => "ok:" + String(v)).catch(e => "ERR:" + e)',
            await_p=True,
        )
        print("  toggle_maximize ->", r.get("result", {}).get("value"))
        r = ev(
            'window.__TAURI_INTERNALS__.invoke("is_maximized")'
            '.then(v => "ok:" + String(v)).catch(e => "ERR:" + e)',
            await_p=True,
        )
        print("  is_maximized ->", r.get("result", {}).get("value"))
        # 还原，避免留下最大化状态
        ev('window.__TAURI_INTERNALS__.invoke("toggle_maximize")', await_p=True)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
