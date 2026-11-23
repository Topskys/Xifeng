"""管线：录音 → (VAD) → ASR → 文本清理 → 注入 → Trace。

M0 的串联顺序与 spec.md §1 状态机一致，不做的部分刻意留空：
- 无 VAD（默认 use_vad=False，对应偏离 D2）
- 纠错默认关（可选本地模型，见 correction.py / G32）
- 无 GUI（M3）

设计约束：
- 每步耗时都进 Trace，不许"顺手优化"而不留证据
- ASR 返回空文本是合法结果 → outcome=Dropped，不注入（G10）
- 注入失败必须改变 outcome，禁止当作成功
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from . import asr, correction, inject as inject_mod, postprocess
from . import vad as vadlib
from .trace import Outcome, Trace, TraceSink


@dataclass
class PipelineConfig:
    sample_rate: int = 16000
    use_vad: bool = False
    do_inject: bool = True
    restore_focus: bool = False
    post: postprocess.PostConfig = field(default_factory=postprocess.PostConfig)
    vad: vadlib.VadConfig = field(default_factory=vadlib.VadConfig)
    # 本地模型纠错（Qwen2.5-0.5B）。默认关：开启后每句 +0.5–1.5s（G32）
    model_correct: bool = False
    # 首尾静音裁剪：治"按住不说话/说完还按着"导致的幻觉与额外 ASR 耗时
    trim_silence: bool = True
    trim: vadlib.TrimConfig = field(default_factory=vadlib.TrimConfig)
    # 注入策略：unicode（快、不动剪贴板）/ clipboard / auto（发完探测不到文本变化就兜底）
    inject_strategy: str = "auto"
    clipboard_restore_ms: int = 500
    # ASR 推理线程数与模型下载源（GUI 配置页同步）
    asr_threads: int = asr.DEFAULT_THREADS
    download_source: str = "modelscope"
    # 本地纠错上下文：config.json 可改最近 N 轮与自定义领域提示
    correction_history_limit: int = correction.DEFAULT_HISTORY_LIMIT
    correction_prompt: str = ""
@dataclass
class Result:
    trace: Trace
    text: str | None          # 清理后的文本（None = 不注入）
    raw: str = ""             # ASR 原文，仅调试用，禁止写日志
    cleaned: str = ""         # 纠错前、清理后的文本（GUI 三级对照的中间级）


def record_seconds(seconds: float, sample_rate: int = 16000) -> np.ndarray:
    """用默认输入设备录固定时长，返回 int16 单声道。

    回调里只 append 到 list（spike 允许一次分配）；正式实现 MUST 用预分配环形缓冲。
    """
    import sounddevice as sd

    frames = int(seconds * sample_rate)
    chunks: list[np.ndarray] = []

    def _cb(indata, frames_, time_, status):  # noqa: ANN001
        if status:
            pass  # 溢出/欠载：spike 只记录不处理
        chunks.append(indata.copy())

    with sd.InputStream(
        samplerate=sample_rate, channels=1, dtype="int16", callback=_cb, blocksize=1024
    ):
        sd.sleep(int(seconds * 1000))

    if not chunks:
        return np.zeros(0, dtype=np.int16)
    pcm = np.concatenate(chunks).reshape(-1).astype(np.int16)
    return pcm[:frames]


def record_interactive(sample_rate: int = 16000) -> np.ndarray:
    """回车开始、回车停止。spike 用，避免引入全局热键依赖。"""
    import sounddevice as sd

    input("按回车开始录音…")
    chunks: list[np.ndarray] = []

    def _cb(indata, frames_, time_, status):  # noqa: ANN001
        chunks.append(indata.copy())

    stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="int16", callback=_cb, blocksize=1024)
    stream.start()
    input("录音中… 按回车停止")
    stream.stop()
    stream.close()

    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks).reshape(-1).astype(np.int16)


def prepare_pcm(
    pcm: np.ndarray,
    *,
    use_vad: bool = False,
    vad: vadlib.SileroVad | None = None,
    vad_cfg: vadlib.VadConfig | None = None,
    trim_silence: bool = True,
    trim_cfg: vadlib.TrimConfig | None = None,
    sample_rate: int = 16000,
) -> tuple[np.ndarray, dict[str, int]]:
    """可选 VAD 切分 + 首尾静音裁剪：`process_pcm`（ptt_loop）与 IPC 运行时
    （D16，`asr_runtime.AsrRuntime`）共用的前半段。

    返回 `(处理后的 PCM, 耗时明细)`，明细键：`ms_vad` / `ms_trim` / `trimmed_ms`。
    全静音或切不出语音段时 PCM 为空数组，调用方按 Dropped 处理。
    算法不在这里实现：切分/重建/裁剪全部复用 `vad` 模块，只保证调用顺序与
    `process_pcm` 历史行为一致。
    """
    pcm = np.asarray(pcm, dtype=np.int16)
    timings = {"ms_vad": 0, "ms_trim": 0, "trimmed_ms": 0}

    # --- 可选 VAD（M1：Silero 双阈值切分；模型缺失回退能量法）
    if use_vad:
        t_vad = time.perf_counter()
        if vad is not None:
            segments = vad.segment(pcm, vad_cfg or vadlib.VadConfig())
        else:
            segments = vadlib.segment(pcm, vad_cfg)
        timings["ms_vad"] = max(0, int((time.perf_counter() - t_vad) * 1000))
        if not segments:
            return np.zeros(0, dtype=np.int16), timings
        pcm = vadlib.rebuild(pcm, segments)

    if len(pcm) == 0:
        return np.zeros(0, dtype=np.int16), timings

    # --- 首尾静音裁剪（在任何推理之前；省掉的不只是时间，还有幻觉）
    if trim_silence:
        t_trim = time.perf_counter()
        pcm, timings["trimmed_ms"] = vadlib.trim(pcm, trim_cfg, sample_rate)
        timings["ms_trim"] = max(0, int((time.perf_counter() - t_trim) * 1000))
        if len(pcm) == 0:
            return np.zeros(0, dtype=np.int16), timings

    return pcm, timings


class Pipeline:
    def __init__(
        self,
        cfg: PipelineConfig | None = None,
        sink: TraceSink | None = None,
        history_provider: object | None = None,
    ) -> None:
        self.cfg = cfg or PipelineConfig()
        self.sink = sink
        self.history_provider = history_provider
        self._counter = 0
        self._vad: vadlib.SileroVad | None = None

    # ------------------------------------------------------------ 生命周期

    def warmup(
        self,
        backend: str = "auto",
        model_dir: str | None = None,
        threads: int = asr.DEFAULT_THREADS,
        language: str = asr.DEFAULT_LANGUAGE,
        use_itn: bool = asr.DEFAULT_USE_ITN,
        download_source: str = "modelscope",
    ) -> None:
        """加载模型 + 空跑一次，消除首包冷启动（G8）。

        `download_source` 当前只记录用户选的模型下载源（M3 做真实下载前用
        来做模型目录提示），本版本模型需手动放置。
        """
        self.cfg.download_source = download_source
        asr.load(
            model_dir=model_dir,
            threads=threads,
            backend=backend,
            language=language,
            use_itn=use_itn,
        )
        asr.warmup()
        # 正则编译、字符串表等一次性开销也必须在 warmup 里付掉
        postprocess.process("嗯，预热一次。", self.cfg.post)
        if self.cfg.model_correct:
            if correction.load(threads=threads):
                ms = correction.warmup()
                print(f"[correct] {correction.describe()} 已热身（{ms}ms）", flush=True)
            else:
                # 模型缺失/加载失败只警告一次，之后按原文注入，绝不逐句重试
                print(
                    f"[correct] 警告：模型纠错不可用：{correction.last_error()}"
                    "（本次关闭纠错，按清理后的原文注入）",
                    flush=True,
                )
                self.cfg.model_correct = False
                # VAD 模型常驻（G8 同理：禁止按次加载）；缺失则回退能量法
        self._vad = vadlib.load_vad()
        if self._vad is not None:
            print("[vad] Silero 已加载（双阈值滞回切分）", flush=True)
        else:
            print("[vad] Silero 模型缺失，回退能量法（放置 models/vad/silero_vad.onnx 启用）", flush=True)

    # ------------------------------------------------------------ 主链路

    def process_pcm(
        self,
        pcm: np.ndarray,
        snap: inject_mod.FocusSnapshot | None = None,
        t_start: float | None = None,
    ) -> Result:
        """对一段 PCM 跑完整链路。t_start 用于把"松键之前的等待"算进 e2e。"""
        cfg = self.cfg
        trace = Trace(id=self._next_id()).start()
        if t_start is not None:
            trace._t0 = t_start  # noqa: SLF001 - Trace 内部字段，spike 直连

        pcm = np.asarray(pcm, dtype=np.int16)
        trace.speech_ms = int(len(pcm) / cfg.sample_rate * 1000)

        # --- 可选 VAD + 首尾静音裁剪（与 IPC 运行时共用，见 prepare_pcm）
        pcm, prep = prepare_pcm(
            pcm,
            use_vad=cfg.use_vad,
            vad=self._vad,
            vad_cfg=cfg.vad,
            trim_silence=cfg.trim_silence,
            trim_cfg=cfg.trim,
            sample_rate=cfg.sample_rate,
        )
        trace.ms_vad = prep["ms_vad"]
        trace.ms_trim = prep["ms_trim"]
        trace.trimmed_ms = prep["trimmed_ms"]
        if len(pcm) == 0:
            trace.outcome = Outcome.DROPPED
            return self._finish(trace, None, "")

        # --- ASR
        t0 = time.perf_counter()
        try:
            res = asr.transcribe(pcm.tobytes(), cfg.sample_rate)
        except asr.AsrError as exc:
            trace.ms_asr = int((time.perf_counter() - t0) * 1000)
            trace.outcome = Outcome.FAILED
            trace.error = str(exc)[:120]
            return self._finish(trace, None, "")
        trace.ms_asr = int((time.perf_counter() - t0) * 1000)
        raw = str(res.get("text", ""))

        # --- 文本清理
        t1 = time.perf_counter()
        cleaned = postprocess.process(raw, cfg.post)
        cleaned_stage = cleaned or ""      # 三级对照的中间级（纠错前）

        # --- 本地模型纠错（可选；守卫拒绝就保持原文，G32）
        if cleaned is not None and cfg.model_correct:
            t_c = time.perf_counter()
            history: list[str] = []
            provider = self.history_provider
            if provider is not None and cfg.correction_history_limit > 0:
                try:
                    history = provider.correction_history(cfg.correction_history_limit)
                except Exception:  # noqa: BLE001 - 历史不可用不能拦住本次输入
                    history = []
            fix = correction.correct(
                cleaned, history=history, custom_prompt=cfg.correction_prompt
            )
            trace.ms_correct = max(0, int((time.perf_counter() - t_c) * 1000))
            if fix.applied:
                cleaned = postprocess.normalize(fix.text, cfg.post)
                trace.fixes = fix.fixes
        if cleaned is not None:
            cleaned = postprocess.final_check(cleaned)

        trace.ms_post = max(0, int((time.perf_counter() - t1) * 1000))

        if cleaned is None:
            trace.outcome = Outcome.DROPPED
            return self._finish(trace, None, raw, cleaned=cleaned_stage)

        trace.chars = len(cleaned)

        if not cfg.do_inject:
            # dry-run：outcome 仍是 Injected（"本可注入"），但 strategy=none 表明没真的发键
            trace.outcome = Outcome.INJECTED
            trace.injection_strategy = "none"
            return self._finish(trace, cleaned, raw, cleaned=cleaned_stage)

        # --- 注入
        t2 = time.perf_counter()
        res = inject_mod.inject_ex(
            cleaned,
            snap,
            inject_mod.InjectConfig(
                strategy=cfg.inject_strategy,
                restore_focus=cfg.restore_focus,
                clipboard_restore_ms=cfg.clipboard_restore_ms,
            ),
        )
        trace.ms_inject = int((time.perf_counter() - t2) * 1000)

        if res.outcome is inject_mod.InjectOutcome.INJECTED:
            trace.outcome = Outcome.INJECTED
        else:
            trace.outcome = Outcome.FAILED
            trace.error = res.outcome.value
        trace.injection_strategy = res.strategy
        return self._finish(trace, cleaned, raw, cleaned=cleaned_stage)

    # ------------------------------------------------------------ 采集 + 处理

    def run_live(self, seconds: float | None = None) -> Result:
        """录一段真实音频并跑完整链路。seconds=None 时用回车控制起止。"""
        snap = inject_mod.snapshot_focus() if self.cfg.do_inject else None
        t_start = None
        if seconds is None:
            pcm = record_interactive(self.cfg.sample_rate)
        else:
            pcm = record_seconds(seconds, self.cfg.sample_rate)
            t_start = None
        return self.process_pcm(pcm, snap, t_start)

    # ------------------------------------------------------------ 内部

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    def _finish(
        self,
        trace: Trace,
        text: str | None,
        raw: str,
        *,
        cleaned: str = "",
    ) -> Result:
        trace.stop()
        if self.sink is not None:
            self.sink.write(trace)
        return Result(trace=trace, text=text, raw=raw, cleaned=cleaned)
