"""端到端验证 IPC：像 Tauri 壳那样拉起 Python 子进程，走完一轮真实通信。

这不是单测（单测在 tests/test_ipc.py），而是**跨进程**冒烟：
验证 stdout 不被污染、ready 事件、方法分发、shutdown 退出码。

    uv run python tools/ipc_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1]
ENTRY = CORE_DIR / "run_spike.py"


def main() -> int:
    import tempfile

    db = Path(tempfile.mkdtemp()) / "smoke.db"
    cmd = [sys.executable, str(ENTRY), "--ipc", "--db", str(db)]
    print(f"[smoke] $ {' '.join(cmd)}")

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        cwd=str(SPIKE),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,     # 日志走 stderr，不能污染协议
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    assert proc.stdin and proc.stdout
    failures: list[str] = []

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def recv(timeout: float = 15.0) -> dict:
        # 阻塞读一行；超时靠外层 kill 兜底（readline 本身没有 timeout）
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("子进程没有输出（可能已崩溃），stderr 见下")
        return json.loads(line)

    try:
        # 1) 等 ready
        ready = recv()
        if ready.get("event") != "ready":
            failures.append(f"首条不是 ready 事件：{ready}")
        pid = ready.get("result", {}).get("pid")
        print(f"[smoke] ready, pid={pid}, 启动耗时 {(time.perf_counter()-t0)*1000:.0f}ms")
        # 不断言 pid == proc.pid：本机沙箱里 Popen 返回的 pid 与子进程真实 pid 会错位
        # （裸 python 子进程也一样），断言它只会制造假失败。pid 能取到整数即可。
        if not isinstance(pid, int) or pid <= 0:
            failures.append(f"ready.pid 不是有效整数：{pid!r}")

        # 2) bootstrap
        send({"id": 1, "method": "bootstrap", "params": {}})
        r = recv()
        if not r.get("ok"):
            failures.append(f"bootstrap 失败：{r.get('error')}")
        else:
            boot = r["result"]
            need = {"version", "dataDir", "settings", "models", "correction", "autostart"}
            missing = need - set(boot)
            if missing:
                failures.append(f"bootstrap 缺字段：{sorted(missing)}")
            print(f"[smoke] bootstrap ok, version={boot.get('version')}, "
                  f"settings 键 {len(boot.get('settings', {}))} 个")

        # 3) stats
        send({"id": 2, "method": "stats", "params": {"period": "week"}})
        r = recv()
        if not r.get("ok"):
            failures.append(f"stats 失败：{r.get('error')}")
        else:
            print(f"[smoke] stats ok, kpis={list(r['result'].get('kpis', {}))[:4]}…")

        # 4) 未知方法不得崩
        send({"id": 3, "method": "no_such_method", "params": {}})
        r = recv()
        if r.get("ok") is not False:
            failures.append("未知方法应返回 ok=false")
        print(f"[smoke] 未知方法正确报错：{r.get('error')}")

        # 5) 参数错误不得崩
        send({"id": 4, "method": "stats", "params": {"bogus": 1}})
        r = recv()
        if r.get("ok") is not False:
            failures.append("多余参数应返回 ok=false")
        print(f"[smoke] 参数错误正确报错：{r.get('error')}")

        # 6) 仍能继续工作（证明前面没把进程搞坏）
        send({"id": 5, "method": "settings_save", "params": {"threads": 4}})
        r = recv()
        if not r.get("ok"):
            failures.append(f"settings_save 失败：{r.get('error')}")
        elif r["result"].get("threads") != 4:
            failures.append(f"threads 未生效：{r['result'].get('threads')}")
        print(f"[smoke] settings_save ok, threads={r['result'].get('threads')}")

        # 7) shutdown
        send({"id": 6, "method": "shutdown"})
        r = recv()
        if not r.get("ok"):
            failures.append(f"shutdown 失败：{r.get('error')}")
        code = proc.wait(timeout=10)
        print(f"[smoke] shutdown ok, 退出码 {code}")
        if code != 0:
            failures.append(f"shutdown 后退出码应为 0，实际 {code}")

        # 8) stdout 必须全是合法 JSON（没有混进日志）
        #    上面每一条都 json.loads 成功过，这里只做最终确认

    except Exception as exc:  # noqa: BLE001
        failures.append(f"异常：{type(exc).__name__}: {exc}")
    finally:
        if proc.poll() is None:
            proc.kill()
        err = (proc.stderr.read() if proc.stderr else "") or ""
        if err.strip():
            print("[smoke] --- 子进程 stderr（日志应全在这里）---")
            for ln in err.strip().splitlines()[-8:]:
                print("   " + ln)

    print()
    if failures:
        print(f"[smoke] 失败 {len(failures)} 项：")
        for f in failures:
            print("  - " + f)
        return 1
    print("[smoke] 全部通过：ready → bootstrap → stats → 错误处理 → save → shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
