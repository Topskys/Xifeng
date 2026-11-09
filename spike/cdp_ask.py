r"""通过 CDP 问 Edge 一句 JS，把 DOM 度量打出来（诊断布局用）。

    python cdp_ask.py "JSON.stringify({iw:innerWidth, dpr:devicePixelRatio})"
MUST 用 Medium 解释器跑（要连 Edge 的调试端口 + 读标题）。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import urllib.request


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


class WS:
    def __init__(self, url: str):
        assert url.startswith("ws://")
        host, rest = url[5:].split("/", 1)
        path = "/" + rest
        self.sock = socket.create_connection((host.split(":")[0], int(host.split(":")[1] or 80)))
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        assert b"101" in resp.split(b"\r\n")[0], resp

    def send(self, obj):
        data = json.dumps(obj).encode()
        header = bytearray([0x81])
        n = len(data)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> dict:
        def read_exact(n):
            buf = b""
            while len(buf) < n:
                buf += self.sock.recv(n - len(buf))
            return buf

        while True:
            b1, b2 = read_exact(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", read_exact(8))[0]
            payload = read_exact(n)
            if opcode == 0x89 or opcode == 0x8A:   # ping/pong：跳过
                continue
            if opcode == 0x88:                     # close
                raise ConnectionError("ws closed")
            return json.loads(payload.decode("utf-8"))

    def call(self, method: str, params: dict, timeout: float = 10.0):
        self.send({"id": 1, "method": method, "params": params})
        self.sock.settimeout(timeout)
        while True:
            msg = self.recv()
            if msg.get("id") == 1:
                return msg


def main() -> int:
    expr = sys.argv[1] if len(sys.argv) > 1 else "document.title"
    tabs = http_json("http://127.0.0.1:9223/json/list")
    page = next(t for t in tabs if t.get("type") == "page")
    ws = WS(page["webSocketDebuggerUrl"])
    res = ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    print(json.dumps(res.get("result", {}).get("result", {}).get("value"), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
