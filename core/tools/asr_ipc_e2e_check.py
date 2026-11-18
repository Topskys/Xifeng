"""手动 e2e：打包核心 + --backend mock，驱动 D16 四个推理方法。"""
import base64
import json
import subprocess
import sys
import time
import math
import struct
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "apps/desktop/src-tauri/resources/core/Xifeng-Core/Xifeng-Core.exe"

# 0.5s 440Hz 正弦，i16 LE
frames = []
for i in range(8000):
    v = int(3000 * math.sin(2 * math.pi * 440 * i / 16000))
    frames.append(struct.pack("<h", v))
pcm = b"".join(frames)
b64 = base64.b64encode(pcm).decode()

reqs = [
    {"id": 1, "method": "asr_begin", "params": {}},
    {"id": 2, "method": "asr_feed", "params": {"pcm_b64": b64}},
    {"id": 3, "method": "asr_end", "params": {}},
    {"id": 4, "method": "asr_feed", "params": {"pcm_b64": b64}},
    {"id": 5, "method": "asr_cancel", "params": {}},
    {"id": 6, "method": "asr_end", "params": {}},
    {"id": 7, "method": "shutdown", "params": {}},
]

t0 = time.perf_counter()
proc = subprocess.Popen(
    [str(CORE), "--ipc", "--backend", "mock"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1,
)
out = [json.loads(proc.stdout.readline())]   # ready
for r in reqs:
    proc.stdin.write(json.dumps(r) + "\n")
    proc.stdin.flush()
    out.append(json.loads(proc.stdout.readline()))
code = proc.wait(timeout=30)
ms = (time.perf_counter() - t0) * 1000

ready = out[0]
assert ready.get("event") == "ready", ready
_, begin, feed, end, feed2, cancel, end2, shutdown = out
print(f"[e2e] 总耗时 {ms:.0f}ms 退出码 {code}")
print(f"[e2e] begin ok={begin['ok']}")
print(f"[e2e] feed frames={feed['result']}")
print(f"[e2e] end={end['result']}")
print(f"[e2e] cancel ok={cancel['ok']}  end2={end2['result']}")
print(f"[e2e] shutdown={shutdown.get('result')}")
assert begin["ok"], begin
assert feed["result"] == {"frames": 8000}, feed
assert end["ok"] and end["result"]["text"] == "呃 今天 我们 嗯 开个会，讨论一下 PyO3 的 接入方案。", end
assert end["result"]["chars"] == len(end["result"]["text"])
assert feed2["result"] == {"frames": 8000}
assert cancel["ok"]
assert end2["result"] == {"text": "", "chars": 0, "ms": 0}, end2
assert shutdown["result"] == {"ok": True}
assert code == 0, code
print("[e2e] 通过：打包核心按契约跑完 asr_begin/feed/end/cancel")
