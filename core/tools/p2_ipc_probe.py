"""P2 手动验证：correct / session_record 走真实 IPC 进程（不打包）。

    uv run python -X utf8 tools/p2_ipc_probe.py

步骤：拉起 run_spike.py --ipc（临时 db）→ bootstrap → correct（真实 LLM，
若模型缺失则验 unavailable 路径）→ session_record → 关库核对行 → shutdown。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SPIKE = Path(__file__).resolve().parent.parent


def main() -> int:
    db = Path(tempfile.mkdtemp(prefix="p2probe-")) / "p2.db"
    payload = "\n".join(
        [
            json.dumps({"id": 1, "method": "bootstrap", "params": {}}),
            json.dumps({"id": 2, "method": "correct",
                        "params": {"text": "今天下午三电开会把方案发给我"}}),
            json.dumps({"id": 3, "method": "session_record", "params": {
                "outcome": "Injected", "chars": 15, "e2e_ms": 900,
                "ms_asr": 500, "ms_post": 120, "ms_inject": 40, "speech_ms": 1300,
                "strategy": "unicode", "fixes": 1,
                "text": "今天下午三点开会把方案发给我",
                "raw_text": "呃 今天下午三电开会把方案发给我",
                "clean_text": "今天下午三电开会把方案发给我",
            }}),
            json.dumps({"id": 4, "method": "shutdown", "params": {}}),
        ]
    ) + "\n"
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "run_spike.py", "--ipc", "--db", str(db)],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        cwd=str(SPIKE), timeout=180, env=env, check=False,
    )
    msgs = [json.loads(l) for l in proc.stdout.splitlines()]
    assert msgs[0].get("event") == "ready", msgs[0]
    by_id = {m.get("id"): m for m in msgs[1:]}

    boot = by_id[1]
    corr_state = boot["result"].get("correction", {})
    print(f"bootstrap.correction = {json.dumps(corr_state, ensure_ascii=False)}")

    correct = by_id[2]
    print(f"correct → ok={correct['ok']} result={json.dumps(correct.get('result', correct.get('error')), ensure_ascii=False)}")

    record = by_id[3]
    print(f"session_record → ok={record['ok']} result={record.get('result')}")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1").fetchall()
    for r in rows:
        d = dict(r)
        print("db row:", json.dumps({k: d[k] for k in (
            "id", "mode", "outcome", "chars", "e2e_ms", "ms_asr", "ms_post",
            "ms_inject", "speech_ms", "fixes", "strategy", "error")}, ensure_ascii=False))
        print("       text 级:", repr(d["text"])[:60], "raw:", repr(d["raw_text"])[:40],
              "clean:", repr(d["clean_text"])[:40])
    conn.close()

    print("stderr 尾部：")
    for line in proc.stderr.splitlines()[-6:]:
        print("   ", line)
    return 0 if proc.returncode == 0 else f"EXIT={proc.returncode}" and 1


if __name__ == "__main__":
    raise SystemExit(main())
