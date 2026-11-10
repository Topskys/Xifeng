"""通过 CDP 在 Tauri 窗口里执行 JS，验证 IPC 真的生效。

为什么要它：截图只能证明"界面有像素"，不能证明数据来自 Rust 桥（py_call）
而不是 HTTP。这里直接读页面内部状态。

    uv run python tools/cdp_probe.py --port 9333
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import urllib.request

PROBES = {
    # 1) Tauri 全局对象注入情况
    "tauri_globals": """() => ({
        hasTauri: typeof window.__TAURI__ !== 'undefined',
        hasInternals: typeof window.__TAURI_INTERNALS__ !== 'undefined',
        invoke: !!(window.__TAURI__?.core?.invoke || window.__TAURI_INTERNALS__?.invoke),
    })""",
    # 3) 页面是否真的画出了侧栏项（说明 React 挂载成功）
    "nav": "() => [...document.querySelectorAll('nav button')].map(b => b.textContent.trim())",
    # 4) 关键：直接调一次 py_call，证明 Rust→Python 通路可用
    "py_call": """async () => {
        const inv = window.__TAURI__?.core?.invoke || window.__TAURI_INTERNALS__?.invoke;
        if (!inv) return { ok: false, reason: 'no invoke' };
        try {
            const r = await inv('py_call', { method: 'bootstrap', params: {} });
            return { ok: true, version: r?.version, keys: r ? Object.keys(r) : [] };
        } catch (e) {
            return { ok: false, reason: String(e) };
        }
    }""",
}


def _ws_connect(url: str):
    """极简 WebSocket 客户端（只要 text frame，够用且不引入依赖）。"""
    import base64
    import os
    import socket

    assert url.startswith("ws://")
    rest = url[5:]
    hostport, path = rest.split("/", 1)
    host, port = hostport.split(":")
    sock = socket.create_connection((host, int(port)), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    if b"101" not in buf.split(b"\r\n")[0]:
        raise RuntimeError(f"握手失败：{buf[:200]!r}")
    return sock


def _send(sock, payload: str) -> None:
    data = payload.encode()
    header = bytearray([0x81])           # FIN + text
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = b"\x00\x00\x00\x00"            # 客户端必须掩码；全 0 等价且简单
    sock.sendall(bytes(header) + mask + data)


def _recv(sock) -> str:
    def readn(n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                raise RuntimeError("连接关闭")
            out += chunk
        return out

    b0, b1 = readn(2)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", readn(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", readn(8))[0]
    return readn(length).decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    args = ap.parse_args()

    with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json", timeout=10) as r:
        targets = json.load(r)
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        print("[cdp] 没有 page 目标", file=sys.stderr)
        return 1
    ws = pages[0]["webSocketDebuggerUrl"]
    print(f"[cdp] 连接 {pages[0]['title']!r} ({pages[0]['url']})")

    sock = _ws_connect(ws)
    failures: list[str] = []
    try:
        for i, (name, expr) in enumerate(PROBES.items(), start=1):
            _send(sock, json.dumps({
                "id": i, "method": "Runtime.evaluate",
                "params": {"expression": f"({expr})()", "awaitPromise": True,
                           "returnByValue": True},
            }))
            while True:
                msg = json.loads(_recv(sock))
                if msg.get("id") == i:
                    break
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                print(f"[cdp] {name}: 异常 {res['exceptionDetails'].get('text')}")
                failures.append(name)
                continue
            val = res.get("result", {}).get("value")
            print(f"[cdp] {name}: {json.dumps(val, ensure_ascii=False)[:300]}")
            if name == "tauri_globals" and isinstance(val, dict) and not val.get("invoke"):
                failures.append("__TAURI__ 未注入 → 前端会回退 HTTP")
            if name == "py_call" and isinstance(val, dict) and not val.get("ok"):
                failures.append(f"py_call 失败：{val.get('reason')}")
            if name == "nav" and not (isinstance(val, list) and len(val) >= 4):
                failures.append(f"侧栏项异常：{val}")
    finally:
        sock.close()

    print()
    if failures:
        print("[cdp] 未通过：")
        for f in failures:
            print("  - " + f)
        return 1
    print("[cdp] 全部通过：__TAURI__ 已注入、py_call 可用、界面已挂载")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
