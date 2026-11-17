"""验证打包出来的 Python 核心（sidecar）能独立跑 IPC。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CORE = (
    Path(__file__).resolve().parents[1]
    / "frontend" / "src-tauri" / "resources" / "core" / "Xifeng-Core" / "Xifeng-Core.exe"
)


def main() -> int:
    if not CORE.is_file():
        print(f"[core-test] 找不到 {CORE}", file=sys.stderr)
        return 1
    print(f"[core-test] {CORE}（{CORE.stat().st_size / 1024 / 1024:.1f} MB）")

    db = Path(tempfile.mkdtemp()) / "core.db"
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [str(CORE), "--ipc", "--db", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )
    assert proc.stdin and proc.stdout
    failures: list[str] = []
    try:
        ready = json.loads(proc.stdout.readline())
        boot_ms = (time.perf_counter() - t0) * 1000
        print(f"[core-test] ready，启动 {boot_ms:.0f}ms，pid={ready.get('result', {}).get('pid')}")
        if ready.get("event") != "ready":
            failures.append(f"首条不是 ready：{ready}")

        for i, (method, params) in enumerate(
            [("bootstrap", {}), ("stats", {"period": "week"})], start=1
        ):
            proc.stdin.write(json.dumps({"id": i, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            r = json.loads(proc.stdout.readline())
            if not r.get("ok"):
                failures.append(f"{method} 失败：{r.get('error')}")
            else:
                extra = (
                    f"version={r['result'].get('version')}"
                    if method == "bootstrap"
                    else f"kpis={len(r['result'].get('kpis', {}))}"
                )
                print(f"[core-test] {method} ok，{extra}")

        proc.stdin.write(json.dumps({"id": 99, "method": "shutdown"}) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
        code = proc.wait(timeout=15)
        print(f"[core-test] shutdown，退出码 {code}")
        if code != 0:
            failures.append(f"退出码 {code}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        if proc.poll() is None:
            proc.kill()
        err = (proc.stderr.read() if proc.stderr else "") or ""
        if err.strip():
            print("[core-test] --- stderr 末几行 ---")
            for line in err.strip().splitlines()[-5:]:
                print("   " + line)

    print()
    if failures:
        print("[core-test] 失败：")
        for f in failures:
            print("  - " + f)
        return 1
    print("[core-test] 通过：打包后的核心能独立跑 IPC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
