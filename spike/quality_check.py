"""真人语音质量验证：读固定句子 → ASR → 算字错率（CER）。

    uv run python quality_check.py                      # 逐句录制（需要麦克风）
    uv run python quality_check.py --wav-dir fixtures/quality   # 复算已有录音
    uv run python quality_check.py --backend sherpa --threads 8

为什么要有它：`run_spike.py` 只能证明"能出字"，证明不了"认得准"。
TTS 合成的音频对 SenseVoice 太友好（几乎全对），真人录音才是真实分布。

判据（本项目自定，非行业标准）：
    CER ≤ 5%   合格
    5% < CER ≤ 15%  可接受（个别同音字/数字改写）
    CER > 15%  有问题，查采集链路或口音/术语覆盖

注意：本项目**关闭 ITN**（`docs/05-gotchas.md` G12）—— ITN 会把"一百五十"写成"150"，
表面形式变了但不算识别错误；更糟的是混排英文会退化（CER 4.5% → 27.3%）。关掉后 ASR 只给原文。
语言 MUST 固定 `zh`：auto 会把普通话判成粤语（G22）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from voiceime_spike import asr, audio
from voiceime_spike.pipeline import record_interactive

SENTENCES = [
    "今天下午三点开会，把方案发给我。",
    "这个接口的延迟必须控制在一百五十毫秒以内。",
    "帮我把 VoiceIME 的 README 更新一下。",
    "会议改到明天上午十点，地点在三楼会议室。",
    "先跑一遍测试，如果全部通过就提交代码。",
]

OUT_DIR = Path(__file__).resolve().parent / "fixtures" / "quality"


def normalize(text: str) -> str:
    """比对口径：去掉标点/空白/大小写差异，只比"字"。

    标点和空格是 ASR 的附带产物，用它们算错没有意义；
    中文标点整体缺失是另一个问题（G9），单独看。
    """
    return "".join(ch for ch in text.lower() if ch.isalnum())


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    r, h = normalize(ref), normalize(hyp)
    if not r:
        return 0.0
    return levenshtein(r, h) / len(r)


def diff_lines(ref: str, hyp: str) -> list[str]:
    """逐字对齐，标出替换/缺失/多余，便于判断是"同音字"还是"整段跑偏"。"""
    import difflib

    r, h = normalize(ref), normalize(hyp)
    out: list[str] = []
    sm = difflib.SequenceMatcher(None, r, h, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append(f"  = {r[i1:i2]}")
        elif tag == "replace":
            out.append(f"  - {r[i1:i2]}  →  + {h[j1:j2]}")
        elif tag == "delete":
            out.append(f"  - {r[i1:i2]}  →  （漏）")
        else:
            out.append(f"  + {h[j1:j2]}  →  （多）")
    return out


def verdict(score: float) -> str:
    if score <= 0.05:
        return "合格"
    if score <= 0.15:
        return "可接受"
    return "有问题"


def refine(text: str, correct: bool = False) -> str:
    """跑一遍注入前真正会做的事：清理 →（可选）模型纠错 → 归一化。

    CER 应该量"用户最终看到的字"，不是模型原始输出。`--no-refine` 可关掉。
    """
    from voiceime_spike import correction, postprocess

    cfg = postprocess.PostConfig()
    cleaned = postprocess.process(text, cfg)
    if cleaned is None:
        return ""
    if correct:
        fix = correction.correct(cleaned)
        if fix.applied:
            cleaned = postprocess.normalize(fix.text, cfg)
    return postprocess.final_check(cleaned) or ""


def run_one(idx: int, ref: str, args: argparse.Namespace) -> tuple[float, int, str]:
    """录一句并识别；返回 (CER, ASR ms, 原文)。"""
    print(f"\n[{idx}/{len(SENTENCES)}] 请朗读：{ref}")
    pcm = record_interactive()
    if pcm.size == 0:
        print("  没采到音频")
        return 1.0, 0, ""

    seconds = len(pcm) / 16000
    path = OUT_DIR / f"{idx:02d}.wav"
    audio.write_wav(path, pcm)
    print(f"  录到 {seconds:.2f}s → {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")

    info = asr.transcribe(pcm.tobytes(), 16000)
    got = info["text"]
    final = refine(got, args.correct) if args.refine else got
    score = cer(ref, final)
    print(f"  ASR {info['ms']}ms · CER {score * 100:.1f}% · {verdict(score)}")
    print(f"  识别：{got}")
    if final != got:
        print(f"  清理后：{final}")
    if score > 0:
        for line in diff_lines(ref, final):
            print("   " + line)
    return score, int(info["ms"]), got


def run_from_wavs(wav_dir: Path, args: argparse.Namespace) -> int:
    results = []
    for idx, ref in enumerate(SENTENCES, 1):
        path = wav_dir / f"{idx:02d}.wav"
        if not path.exists():
            print(f"[{idx}] 缺少 {path}，跳过")
            continue
        pcm, rate = audio.read_wav(path)
        if rate != 16000:
            print(f"[{idx}] 采样率 {rate} ≠ 16000，跳过")
            continue
        info = asr.transcribe(pcm.tobytes(), 16000)
        raw = info["text"]
        final = refine(raw, args.correct) if args.refine else raw
        score = cer(ref, final)
        results.append((idx, ref, raw, score, int(info["ms"])))
        print(f"\n[{idx}] 参考：{ref}")
        print(f"    识别：{raw}")
        print(f"    CER {score * 100:.1f}% · {verdict(score)} · ASR {info['ms']}ms")
        if final != raw:
            print(f"    清理后：{final}（原始 CER {cer(ref, raw) * 100:.1f}%）")
        if score > 0:
            for line in diff_lines(ref, final):
                print("   " + line)
    return summarize(results, args)


def summarize(results: list[tuple], args: argparse.Namespace) -> int:
    if not results:
        print("没有可评估的样本")
        return 2
    total_cer = sum(r[3] for r in results) / len(results)
    perfect = sum(1 for r in results if r[3] == 0)
    ms = sorted(r[4] for r in results)

    print("\n" + "=" * 60)
    print(f"句数 {len(results)} · 完全正确 {perfect}/{len(results)} · 平均 CER {total_cer * 100:.1f}%")
    print(f"ASR 耗时 P50 {ms[len(ms) // 2]}ms / 最慢 {ms[-1]}ms · 后端 {asr.backend()}")
    print(f"总体判定：{verdict(total_cer)}")
    print("=" * 60)
    print("参考：CER 只统计字，标点不计；ITN 已关闭（G12），语言固定 zh（G22）。")
    return 0 if total_cer <= 0.15 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="真人语音质量验证（CER）")
    p.add_argument("--backend", default="auto", choices=["auto", "mock", "sherpa"])
    p.add_argument("--model-dir", default=None)
    p.add_argument("--threads", type=int, default=asr.DEFAULT_THREADS)
    p.add_argument("--language", default=asr.DEFAULT_LANGUAGE,
                   help="ASR 语言，默认 zh（auto 会把普通话误判成粤语，见 G22）")
    p.add_argument("--itn", action="store_true",
                   help="开启 ITN（默认关；开了会把数字改写成阿拉伯数字，且混排英文退化）")
    p.add_argument("--wav-dir", default=None, help="复算已有录音目录（每句 01.wav…05.wav）")
    p.add_argument("--no-refine", dest="refine", action="store_false",
                   help="CER 只算 ASR 原始输出，不套清理/纠错")
    p.add_argument("--correct", action="store_true",
                   help="CER 里计入本地模型纠错（默认关；与运行时默认一致）")
    p.set_defaults(refine=True)
    args = p.parse_args(argv)

    asr.load(
        model_dir=args.model_dir,
        threads=args.threads,
        backend=args.backend,
        language=args.language,
        use_itn=args.itn,
    )
    asr.warmup()
    print(f"[asr] 实际后端={asr.backend()} language={args.language} itn={args.itn}")
    if args.correct:
        from voiceime_spike import correction

        if correction.load(threads=args.threads):
            print(f"[correct] {correction.describe()} 热身 {correction.warmup()}ms")
        else:
            print(f"[correct] ⚠ {correction.last_error()}（本次不计入纠错）")
            args.correct = False
    if asr.backend() == "mock":
        print("[warn] mock 后端返回固定文本，CER 无意义。先装依赖并下载模型。")
        return 2

    if args.wav_dir:
        return run_from_wavs(Path(args.wav_dir), args)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"录音保存到 {OUT_DIR}")
    print("提示：正常语速、离麦 20–30cm、环境保持平时使用时的噪声水平。")

    results = []
    for idx, ref in enumerate(SENTENCES, 1):
        score, ms, got = run_one(idx, ref, args)
        results.append((idx, ref, got, score, ms))
    return summarize(results, args)


if __name__ == "__main__":
    sys.exit(main())
