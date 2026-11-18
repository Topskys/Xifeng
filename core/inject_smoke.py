"""注入 smoke（自动化版）：开一个 Tk 窗口当"记事本"，注入后读回比对。

    uv run python inject_smoke.py
    uv run python inject_smoke.py --text "你好😀世界"

为什么要它：spec A0-1 要求"连续 10 次在记事本里正确出字"，纯手测不可复现。
Tk 的 Text 控件是标准 Win32 窗口，能吃到 `SendInput(UNICODE)`，读回也不会骗人。

**注入必须在 worker 线程里做**（见 docs/05-gotchas.md G15）：
SendInput 把消息投给目标窗口所在线程；若在本进程主线程里批量注入，
Tk 主线程正卡在 sleep 上没法处理自己的消息队列，队列溢出就会丢字——
这是测试自伤，不是注入缺陷。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from xifeng import inject as inj

CASES = [
    "你好，世界",
    "中文 English 混排 123",
    "emoji 😀🎉 也不能乱码",
    "第一行\n第二行",
    "长文本：" + "测试注入是否丢字。" * 20,
]


def run_case(text: str, chunk: int = inj.CHUNK_UNITS) -> tuple[bool, str, str, float]:
    """返回 (是否一致, 实际读回, 失败原因, 注入耗时 ms)。

    线程分工：
    - worker 线程只做"抢焦点 + 发键"，不碰任何 Tk 控件（Tk 非线程安全）
    - 主线程用 root.after 轮询读回，消息泵始终在转（G15）
    """
    import tkinter as tk

    result: dict[str, object] = {"ok": False, "got": "", "why": "", "ms": 0.0}
    state: dict[str, object] = {"done": False, "outcome": None, "why": "", "ms": 0.0}

    root = tk.Tk()
    root.title("Xifeng 注入 smoke")
    root.geometry("700x300+200+200")
    widget = tk.Text(root, font=("Microsoft YaHei UI", 11))
    widget.pack(fill="both", expand=True)
    widget.focus_set()

    def worker() -> None:
        hwnd = int(root.wm_frame(), 16)
        # 抢前台：SetForegroundWindow 直接调用会随机失败，走 ALT 技巧（G6）
        inj.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        for _ in range(20):
            cur = inj.snapshot_focus()
            if cur is not None and cur.hwnd == hwnd:
                break
            inj._restore_focus(hwnd)  # noqa: SLF001
            time.sleep(0.1)

        snap = inj.snapshot_focus()
        if snap is None or snap.hwnd != hwnd:
            state["why"] = "测试窗口拿不到焦点（自动化 shell 里必现，见 G14）"
        else:
            t0 = time.perf_counter()
            state["outcome"] = inj.inject(text, snap, inj.InjectConfig(chunk_units=chunk))
            state["ms"] = (time.perf_counter() - t0) * 1000
        state["done"] = True

    def finish(got: str, why: str = "") -> None:
        result["got"] = got
        result["ms"] = state["ms"]
        result["ok"] = got == text
        result["why"] = why or ("" if result["ok"] else f"读回不一致（期望 {len(text)} 字，实际 {len(got)} 字）")
        root.destroy()

    deadline = time.time() + 6
    last: list[str] = [""]
    stable = 0

    def poll() -> None:
        nonlocal stable
        if not state["done"]:
            if time.time() > deadline:
                finish(widget.get("1.0", "end-1c"), "注入未返回（超时）")
            else:
                root.after(50, poll)
            return

        if state["why"]:
            finish(widget.get("1.0", "end-1c"), str(state["why"]))
            return
        outcome = state["outcome"]
        if outcome is not inj.InjectOutcome.INJECTED:
            finish(widget.get("1.0", "end-1c"), outcome.value if outcome else "无结果")
            return

        # 等消息队列排空：内容连续 2 次（200ms）不变才判定
        cur = widget.get("1.0", "end-1c")
        stable = stable + 1 if cur == last[0] else 0
        last[0] = cur
        if stable >= 2:
            finish(cur)
        elif time.time() > deadline:
            finish(cur)
        else:
            root.after(100, poll)

    root.after(600, lambda: threading.Thread(target=worker, daemon=True).start())
    root.after(700, poll)
    root.mainloop()
    return bool(result["ok"]), str(result["got"]), str(result["why"]), float(result["ms"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="注入 smoke：Tk 窗口读回比对")
    p.add_argument("--text", action="append", help="只测指定文本，可重复")
    p.add_argument("--no-window", action="store_true", help="只打印用例，不开窗口")
    p.add_argument("--chunk", type=int, default=inj.CHUNK_UNITS, help="每批码元数（默认 32；丢字时试 8）")
    args = p.parse_args(argv)

    cases = args.text or CASES
    print(inj.describe())
    if args.no_window:
        for c in cases:
            print(" -", repr(c))
        return 0

    passed = 0
    for i, text in enumerate(cases, 1):
        ok, got, why, ms = run_case(text, args.chunk)
        mark = "PASS" if ok else "FAIL"
        print(f"[{i}/{len(cases)}] {mark} 期望 {len(text)} 字，实际 {len(got)} 字 · inject={ms:.0f}ms {('· ' + why) if why else ''}")
        if not ok:
            print(f"       期望: {text[:60]!r}")
            print(f"       实际: {got[:60]!r}")
        else:
            passed += 1

    print(f"\n[smoke] {passed}/{len(cases)} 通过")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
