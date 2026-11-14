"""便携壳 e2e：CDP 驱动 voice_test，合成按键走真实 WH_KEYBOARD_LL 链路。

用法：uv run python tools/voice_e2e.py 9334

序列（SendInput 不带 MAGIC，验的是完整链路，G20 语义成立）：
  1. tap×2 —— 双击触发 + 立刻松开：hold≈150ms < 250ms → 误触 → asr_cancel
     （tap 的 150ms 保持会被 asr_begin IPC + cpal 开流吃掉，hold 可能记 0——预期）
  2. tap + down 1.2s + up —— 正常会话：asr_begin → 开麦 → asr_feed → drain 120ms
     → asr_end（无语音 → 空文本 → 后处理判不注入）
  3. down 1.2s（单击长按，无双击）—— 不应触发任何会话（双击语义对照）

每步后读壳的 stderr 日志尾部，人工/自动核对关键行。
"""

import json
import sys
import time
import urllib.request

sys.path.insert(0, "tools")
from cdp_probe import _recv, _send, _ws_connect  # noqa: E402

def voice_test(port: str, mode: str) -> str:
    js = f"""
    (async () => {{
      try {{
        const t0 = performance.now();
        await window.__TAURI_INTERNALS__.invoke("voice_test", {{ mode: "{mode}" }});
        return "OK " + Math.round(performance.now() - t0) + "ms";
      }} catch (e) {{
        return "ERR: " + String(e);
      }}
    }})()
    """
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=8) as r:
        page = [x for x in json.load(r) if x.get("type") == "page"][0]
    sock = _ws_connect(page["webSocketDebuggerUrl"])
    try:
        _send(sock, json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": js, "awaitPromise": True,
                       "returnByValue": True},
        }))
        while True:
            m = json.loads(_recv(sock))
            if m.get("id") == 1:
                break
        return m["result"]["result"].get("value", "?")
    finally:
        sock.close()

def tail_stderr(path: str, n: int = 12) -> None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[-n:]
        for line in lines:
            print("    " + line)
    except OSError as e:
        print(f"    (读 stderr 失败：{e})")

def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "9334"
    stderr_log = r"D:\env\tmp\shell-stderr.log"

    print("== 1. tap x2（双击触发后立刻松开 → 误触 → asr_cancel）==")
    print("  invoke tap:", voice_test(port, "tap"))
    time.sleep(0.05)
    print("  invoke tap:", voice_test(port, "tap"))
    time.sleep(2.0)
    tail_stderr(stderr_log, 5)

    print("== 2. tap + down 1.2s + up（tap+down = 双击第二次按下并保持 → 正常会话）==")
    print("  invoke tap:", voice_test(port, "tap"))
    time.sleep(0.05)
    print("  invoke down:", voice_test(port, "down"))
    time.sleep(1.2)
    print("  invoke up:", voice_test(port, "up"))
    time.sleep(3.0)  # 等 asr_end + 后处理 + 注入判定走完
    tail_stderr(stderr_log, 16)

    print("== 3. down 1.2s（单击长按，无双击 → 不触发，对照组）==")
    print("  invoke down:", voice_test(port, "down"))
    time.sleep(1.2)
    print("  invoke up:", voice_test(port, "up"))
    time.sleep(2.0)
    tail_stderr(stderr_log, 5)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
