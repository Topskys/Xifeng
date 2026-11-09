"""延迟基准：跑 N 次同一段音频，输出各阶段 P50/P95/P99 并与预算比对。

    uv run python bench.py --n 20 --wav sample.wav
    uv run python bench.py --n 20 --seconds 3 --backend mock --no-inject

判据来自 docs/01-overview/latency-budget.md：
    ASR ≤ 350ms / postprocess < 1ms / inject ≤ 10ms / e2e P95 ≤ 1500ms（PTT 目标 800ms）

注意：mock 后端的 ASR 耗时恒为 0，只能验证管线开销，不能验证推理。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import numpy as np

from voiceime_spike import asr, audio, benv
from voiceime_spike.pipeline import Pipeline, PipelineConfig, record_seconds
from voiceime_spike.trace import percentiles

BUDGET = {"ms_asr": 350, "ms_post": 1, "ms_inject": 10, "e2e_ms": 1500, "e2e_ptt": 800}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VoiceIME 延迟基准")
    p.add_argument("--n", type=int, default=20, help="重复次数（默认 20）")
    p.add_argument("--wav", default=None, help="输入 wav（不填则合成 3s 音频）")
    p.add_argument("--seconds", type=float, default=3.0, help="合成音频时长（无 --wav 时）")
    p.add_argument("--backend", default="auto", choices=["auto", "mock", "sherpa"])
    p.add_argument("--model-dir", default=None)
    p.add_argument("--threads", type=int, default=8, help="ONNX 线程数（8 核实测 8 最快）")
    p.add_argument("--language", default=asr.DEFAULT_LANGUAGE,
                   help="ASR 语言，默认 zh（auto 会把普通话误判成粤语，见 G22）")
    p.add_argument("--itn", action="store_true",
                   help="开启 ITN（默认关；见 metrics.md 的口径说明）")
    p.add_argument("--vad", action="store_true")
    p.add_argument("--inject", action="store_true", help="同时测注入（会真的往光标打字，危险）")
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


def load_pcm(args: argparse.Namespace) -> np.ndarray:
    if args.wav:
        pcm, rate = audio.read_wav(args.wav)
        if rate != 16000:
            raise SystemExit(f"wav 采样率必须是 16000，实际 {rate}")
        return pcm
    return audio.synth_speech_like(args.seconds)


def summarize(values: list[int]) -> dict[str, int]:
    p = percentiles(values)
    p["mean"] = int(statistics.fmean(values)) if values else 0
    p["n"] = len(values)
    return p


def fmt_row(name: str, p: dict[str, int], budget: int | None, note: str = "") -> str:
    p50, p95, p99 = p["p50"], p["p95"], p["p99"]
    if budget is None:
        verdict = "—"
    else:
        verdict = "PASS" if p95 <= budget else "FAIL"
    if note:
        verdict += note
    b = "-" if budget is None else str(budget)
    return f"{name:<12}{p50:>7}{p95:>7}{p99:>7}{b:>9}   {verdict}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    env = benv.probe()
    print(f"[bench] 环境：{env.line()}")
    print(f"[bench] 判档：{benv.TIER_NOTE[env.tier]}")
    if not env.trusted():
        print("[bench] [WARN] 本组数字不可信（高载或内存紧张）。建议关掉浏览器/IDE 后重跑。")

    cfg = PipelineConfig(use_vad=args.vad, do_inject=args.inject)
    pipe = Pipeline(cfg)

    print(f"[bench] n={args.n} backend={args.backend} inject={args.inject} vad={args.vad}")
    pipe.warmup(
        backend=args.backend,
        model_dir=args.model_dir,
        threads=args.threads,
        language=args.language,
        use_itn=args.itn,
    )
    backend = asr.backend()
    print(f"[bench] 实际后端={backend} language={args.language} itn={args.itn}")

    pcm = load_pcm(args)
    seconds = len(pcm) / cfg.sample_rate
    print(f"[bench] 音频 {seconds:.2f}s")

    if backend == "mock":
        print("[bench] 警告：mock 后端，ASR 耗时恒为 0，数字只反映管线开销")

    if args.inject:
        print("[bench] 警告：--inject 会真的向当前窗口打字，请把光标放到记事本里")

    rows: list[dict[str, object]] = []
    for i in range(args.n):
        r = pipe.process_pcm(pcm)
        t = r.trace
        rows.append(
            {
                "i": i + 1,
                "outcome": t.outcome.value,
                "ms_asr": t.ms_asr,
                "ms_post": t.ms_post,
                "ms_inject": t.ms_inject,
                "e2e_ms": t.e2e_ms,
                "chars": t.chars,
                "error": t.error,
            }
        )

    ok = [r for r in rows if r["outcome"] == "Injected"]
    print(f"[bench] 成功 {len(ok)}/{len(rows)}")
    if not ok:
        print("[bench] 全部失败，无法统计延迟", file=sys.stderr)
        return 1

    stats = {
        "asr": summarize([int(r["ms_asr"]) for r in ok]),
        "post": summarize([int(r["ms_post"]) for r in ok]),
        "inject": summarize([int(r["ms_inject"]) for r in ok]),
        "e2e": summarize([int(r["e2e_ms"]) for r in ok]),
    }

    if args.json:
        print(json.dumps({
            "backend": backend,
            "seconds": round(seconds, 2),
            "env": {
                "cpu_pct": None if env.cpu_pct is None else round(env.cpu_pct, 1),
                "mem_free_mb": env.mem_free_mb,
                "mem_total_mb": env.mem_total_mb,
                "commit_used_mb": env.commit_used_mb,
                "tier": env.tier,
                "trusted": env.trusted(),
                "asr_budget": env.asr_budget(BUDGET["ms_asr"]),
            },
            **stats,
        }, ensure_ascii=False, indent=2))
        return 0

    print()
    print(f"{'stage':<12}{'p50':>7}{'p95':>7}{'p99':>7}{'budget':>9}   verdict")
    print("-" * 52)
    asr_budget = env.asr_budget(BUDGET["ms_asr"])
    untrusted = "  (不可信)" if not env.trusted() else ""
    print(fmt_row("asr", stats["asr"], asr_budget, untrusted))
    print(fmt_row("post", stats["post"], BUDGET["ms_post"]))
    print(fmt_row("inject", stats["inject"], BUDGET["ms_inject"] if args.inject else None))
    print(fmt_row("e2e", stats["e2e"], BUDGET["e2e_ms"], untrusted))
    print()
    print(f"[bench] e2e mean={stats['e2e']['mean']}ms  (PTT 目标 P50≤{BUDGET['e2e_ptt']}ms)")
    print(f"[bench] 环境快照：{env.line()} · 档位={env.tier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
