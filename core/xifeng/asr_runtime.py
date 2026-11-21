"""D16 推理运行时：IPC 侧的「缓冲 + 裁剪 + ASR 解码」。

Rust 壳接管采集/注入/后处理后（决策 D16，docs/00-process/plan.md），
Python 核心只暴露推理与记账方法（契约见 docs/03-core-concepts/ipc-contract.md）：

    asr_begin        清空缓冲，确保模型已加载（首次调用会加载，2~4s，调用方容忍 30s）
    asr_feed         追加 16kHz 单声道 i16 LE 裸字节（base64），返回累计帧数
    asr_end          静音裁剪/VAD + ASR 解码，返回原始文本（未经 postprocess）
    asr_cancel       丢弃缓冲（Esc 取消本次）
    correct          本地 LLM 纠错一句文本（G32 守卫在 correction.check 内）
    session_record   把一次会话写进历史库（store_text 开关在 add_history 内生效）

设计要点：
- 模型延迟加载 + 常驻：`serve()` 发出 ready 后由后台线程跑 `warmup()`，
  绝不阻塞协议主循环；首次 `asr_begin` 一般已不用等冷启动（G8）
- 缓冲只在 IPC 主循环（单线程顺序处理）里访问，无需加锁；唯一的并发点是
  模型加载/解码 vs warmup 线程，统一用 `_load_lock` 串行化（ASR 与 LLM 都在
  这把锁下：recognizer 不保证线程安全，llama.cpp 同理）
- 裁剪/VAD/解码复用现有代码（`pipeline.prepare_pcm` + `asr.transcribe`），
  与 ptt_loop 路径逐字节同源，这里不重写任何算法
- end 返回的是 ASR 原始输出；后处理与注入由 Rust 壳负责
- 日志只记长度与耗时，禁止出现识别文本全文（仓库硬约束）
"""

from __future__ import annotations

import base64
import binascii
import sys
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

from . import asr, correction, vad as vadlib
from .pipeline import prepare_pcm
from .trace import Outcome, Trace

if TYPE_CHECKING:
    from .store import Store

SAMPLE_RATE = 16000
MAX_SECONDS = 60
#: 缓冲上限（帧 = i16 采样数）：60s ≈ 960000 帧，超出丢弃新数据
MAX_FRAMES = MAX_SECONDS * SAMPLE_RATE

#: 支持的识别语言（与 `settings.LANGUAGES` 同源；SenseVoice 的语种清单）。
#: G22：auto 会把普通话判成粤语，必须显式指定。
SUPPORTED_LANGUAGES = ("zh", "en", "ja", "ko", "yue")


def _log(msg: str) -> None:
    """日志只走 stderr —— stdout 是 IPC 协议专用通道。"""
    try:
        print(f"[asr-runtime] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - stderr 不可写时也不能影响协议
        pass


def _as_int(value: object, key: str) -> int:
    """session_record 的整数参数：类型不对要显式报错，不靠 int() 猜。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"session_record.{key} 必须是整数")
    return int(value)


class AsrRuntime:
    """一次 utterance 的缓冲与解码状态。

    每个 IPC 进程一个实例（`ipc.serve` 创建/接收）。四个公开方法
    （`begin` / `feed` / `end` / `cancel`）只被 IPC 主循环单线程调用。
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        backend: str = "auto",
        model_dir: str | None = None,
        threads: int = asr.DEFAULT_THREADS,
        language: str = asr.DEFAULT_LANGUAGE,
        use_itn: bool = asr.DEFAULT_USE_ITN,
        use_vad: bool = False,
        trim_silence: bool = True,
        vad_cfg: vadlib.VadConfig | None = None,
        trim_cfg: vadlib.TrimConfig | None = None,
        autostart_warmup: bool = True,
        model_correct: bool = False,
        store: Store | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.backend = backend
        self.model_dir = model_dir
        self.threads = threads
        self.language = language
        self.use_itn = use_itn
        self.use_vad = use_vad
        self.trim_silence = trim_silence
        self.vad_cfg = vad_cfg or vadlib.VadConfig()
        self.trim_cfg = trim_cfg or vadlib.TrimConfig()
        #: serve() 是否在 ready 后自动起 warmup 线程（测试/懒加载路径可关）
        self.autostart_warmup = autostart_warmup
        #: 用户设置「本地模型纠错」：warmup 预热 LLM，correct 时零冷启动（G32 守卫在 correction 内）
        self.model_correct = model_correct
        #: 历史库（--no-db 时是 :memory: 实例，永远非 None；None 只出现在测试直构）
        self._store = store

        self._buf: list[np.ndarray] = []
        self._frames = 0
        self._overflow_warned = False
        self._load_lock = threading.Lock()
        self._loaded = False
        # 纠错模型的就绪标记：加载失败是粘性的，绝不逐句重试（pipeline 同款语义）
        self._correct_ready = False
        self._vad: vadlib.SileroVad | None = None
        self._vad_ready = False
        # warmup 线程：shutdown 时主循环会 join 它再退出，
        # 避免守护线程停在 2~4s 的原生模型加载里把进程退出搞挂
        self._warmup_stop = threading.Event()
        self._warmup_thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期

    def ensure_loaded(self) -> None:
        """加载 ASR 模型（幂等、线程安全）。warmup 线程与 `asr_begin` 共用。

        加载失败抛 `asr.AsrError`，由 IPC 层转成 ok=false，不崩进程。
        """
        with self._load_lock:
            self._load_locked()

    def _load_locked(self) -> None:
        """调用方必须已持有 `_load_lock`。"""
        if self._loaded:
            return
        asr.load(
            model_dir=self.model_dir,
            threads=self.threads,
            backend=self.backend,
            language=self.language,
            use_itn=self.use_itn,
        )
        self._loaded = True

    def set_language(self, language: str) -> dict:
        """`set_language` params={language} → {language}：切换识别语言。

        换语言必须**重建 recognizer**（sherpa 的 language 是构造参数，
        不能热改），所以这里同步重载模型——耗时与首次加载同量级（数秒）。

        非法语言显式报错（不静默忽略）：用户点了没反应是最糟的体验。
        重建逻辑与设置热更共用 `apply_settings`（单一实现）。
        """
        if not isinstance(language, str) or not language.strip():
            raise ValueError("set_language.language 必须是字符串")
        self.apply_settings(language=language)
        return {"language": self.language}

    def apply_settings(
        self,
        *,
        use_vad: bool | None = None,
        trim_silence: bool | None = None,
        model_correct: bool | None = None,
        threads: int | None = None,
        model_dir: str | None = None,
        language: str | None = None,
    ) -> dict:
        """把用户设置同步进运行时（P2-② 热更；`ipc._sync_runtime` 调用）。

        两类参数分开处理：

        - **非构造参数**（录音段切分、首尾静音裁剪、模型纠错开关）：只是
          每次 `end()` / `correct()` 时读的开关，改属性即可，下一次说话生效。
        - **构造参数**（语言、推理线程数、模型目录）：sherpa 的 recognizer
          在构造时就把它们吃掉了，改了必须重建（数秒）。这里在同一次调用里
          做完，用户点「保存」就生效，不必重启。

        没变就不重建（重载要几秒，不能白付）。非法语言显式报错，不静默忽略。

        如实记账：线程数改动作用于 ASR；**已加载的纠错 LLM 保持原线程数**
        直到进程重启（llama.cpp 的线程池在构造时定死，热改要重载整个模型）。

        返回 `{"reloaded": bool, "language": str, "threads": int}`。
        """
        if use_vad is not None:
            self.use_vad = bool(use_vad)
        if trim_silence is not None:
            self.trim_silence = bool(trim_silence)
        if model_correct is not None:
            self.model_correct = bool(model_correct)

        lang: str | None = None
        if language is not None:
            lang = str(language).strip().lower()
            if lang not in SUPPORTED_LANGUAGES:
                raise ValueError(
                    f"不支持的识别语言：{language}（支持：{', '.join(SUPPORTED_LANGUAGES)}）"
                )
        new_threads = self.threads if threads is None else int(threads)
        new_dir = self.model_dir if model_dir is None else (str(model_dir) or None)

        changed = (
            (lang is not None and lang != self.language)
            or new_threads != self.threads
            or new_dir != self.model_dir
        )
        if not changed:
            return {"reloaded": False, "language": self.language, "threads": self.threads}

        if lang is not None:
            self.language = lang
        self.threads = new_threads
        self.model_dir = new_dir
        reloaded = False
        with self._load_lock:
            if self._loaded:
                # 已加载过才值得重建：懒加载路径改属性就够，首次 load 自然用新值
                self._loaded = False
                self._load_locked()
                reloaded = True
        _log(
            f"设置已热更：language={self.language} threads={self.threads} "
            f"model_dir={self.model_dir or '(默认)'} reloaded={reloaded}"
        )
        return {"reloaded": reloaded, "language": self.language, "threads": self.threads}

    def warmup(self) -> None:
        """后台线程入口：加载 + 空跑一次，消除首包冷启动（G8）。

        整段持 `_load_lock`：空跑解码也不与主循环的 `asr_end` 并发
        （sherpa 的 recognizer 不保证线程安全）。失败只记 stderr，
        绝不抛出 —— 后台线程的死活不影响协议，首次 `asr_begin` 会再试。
        """
        try:
            if self._warmup_stop.is_set():      # shutdown 先到：直接收工
                return
            with self._load_lock:
                self._load_locked()
                if self._warmup_stop.is_set():  # 加载完就停，不再空跑
                    return
                asr.warmup()
                if self.use_vad:
                    self._ensure_vad()
                if self.model_correct:
                    # LLM 冷启动同样付在启动阶段；失败只警告，correct 时按原文放行
                    self._ensure_correction_locked()
            suffix = f" + {correction.describe()}" if self.model_correct else ""
            _log(f"warmup 完成 backend={asr.backend()}{suffix}")
        except Exception as exc:  # noqa: BLE001 - 后台线程绝不带崩进程
            _log(f"⚠ warmup 失败（懒加载兜底，首次 asr_begin 会重试）：{type(exc).__name__}: {exc}")

    def start_warmup(self) -> threading.Thread:
        """ready 之后起后台 warmup 线程（不阻塞协议主循环）。返回线程句柄。"""
        t = threading.Thread(target=self.warmup, name="asr-warmup", daemon=True)
        self._warmup_thread = t
        t.start()
        return t

    def stop_warmup(self, timeout: float = 30.0) -> None:
        """进程退出前收 warmup 线程：置停止位并 join，保证退出码干净。

        线程最多在当前阶段（加载/空跑）结束点停下；模型加载中最多等
        数秒，换来的是进程退出时不与解释器收尾竞争（daemon 线程在原生
        调用中被杀可能挂住退出）。
        """
        self._warmup_stop.set()
        t = self._warmup_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _ensure_vad(self) -> None:
        """Silero 模型常驻（G8 同理：禁止按次加载）；缺失回退能量法。"""
        if not self._vad_ready:
            self._vad = vadlib.load_vad()
            self._vad_ready = True

    def _ensure_correction_locked(self) -> None:
        """加载 + 热身纠错模型（调用方必须已持有 `_load_lock`）。幂等。

        加载失败是粘性的：置 `_correct_ready` 后不再逐句重试，
        `correction.correct` 对未加载状态会按 unavailable 返回原文（G32）。
        """
        if self._correct_ready or correction.loaded():
            self._correct_ready = True
            return
        if correction.load(threads=self.threads):
            ms = correction.warmup()
            _log(f"correct 模型已热身（{ms}ms）")
        else:
            _log(f"⚠ correct 模型不可用：{correction.last_error()}（按原文注入）")
        self._correct_ready = True

    # ------------------------------------------------------------ 协议方法

    def begin(self) -> dict:
        """`asr_begin` params={} → {}：清空缓冲 + 确保模型已加载。"""
        self.ensure_loaded()
        self._clear()
        return {}

    def feed(self, pcm_b64: str) -> dict:
        """`asr_feed` params={pcm_b64} → {frames}：追加 i16 LE 裸字节。

        返回累计帧数（=已接受字节数/2）。缓冲满 60s 后丢弃新数据，
        只警告一次（不刷屏）。
        """
        if not isinstance(pcm_b64, str):
            raise ValueError("asr_feed.pcm_b64 必须是字符串")
        try:
            raw = base64.b64decode(pcm_b64, validate=False)  # 容忍换行
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"asr_feed.pcm_b64 不是合法 base64: {exc}") from exc
        if len(raw) % 2:
            raise ValueError("asr_feed.pcm_b64 字节长度必须是偶数（i16）")

        frames = len(raw) // 2
        if frames:
            if self._frames + frames > MAX_FRAMES:
                if not self._overflow_warned:
                    self._overflow_warned = True
                    _log(f"缓冲超过 {MAX_SECONDS}s 上限，丢弃新到的 {frames} 帧")
            else:
                # frombuffer 是只读视图，astype 拷一份，与调用方的字节缓冲解耦
                self._buf.append(np.frombuffer(raw, dtype="<i2").astype(np.int16))
                self._frames += frames
        return {"frames": self._frames}

    def end(self) -> dict:
        """`asr_end` params={} → {text, chars, ms}：裁剪 + 解码。

        返回 ASR 原始输出（未经 postprocess，空串合法）；返回后清空缓冲。
        ms 是解码耗时（含 VAD/裁剪）。日志只记长度与耗时。
        """
        frames = self._frames
        pcm = self._take()
        t0 = time.perf_counter()
        text = ""
        if pcm.size:
            if self.use_vad:
                self._ensure_vad()
            prepared, _ = prepare_pcm(
                pcm,
                use_vad=self.use_vad,
                vad=self._vad,
                vad_cfg=self.vad_cfg,
                trim_silence=self.trim_silence,
                trim_cfg=self.trim_cfg,
                sample_rate=self.sample_rate,
            )
            if prepared.size:
                # 持锁解码：与 warmup 线程的空跑互斥（recognizer 非线程安全）
                with self._load_lock:
                    res = asr.transcribe(prepared.tobytes(), self.sample_rate)
                text = str(res.get("text", ""))
        ms = max(0, int((time.perf_counter() - t0) * 1000))
        # 硬约束：只记长度与耗时，禁止出现识别文本
        _log(f"asr_end 完成：in_frames={frames} chars={len(text)} ms={ms}")
        return {"text": text, "chars": len(text), "ms": ms}

    def cancel(self) -> dict:
        """`asr_cancel` params={} → {}：丢弃缓冲（Esc 取消本次）。"""
        self._take()
        return {}

    def correct(self, text: str) -> dict:
        """`correct` params={text} → {applied, text, fixes, reason, ms}。

        本地 LLM 纠错一句文本。守卫（删词/大改/解释性输出）在
        `correction.check` 内实现，`applied=False` 即拒绝——壳只需看
        `applied` 才采用 `text`，其余情况 `text` 恒为原文。首调用先付
        模型加载（warmup 线程已预热时是空操作）；持 `_load_lock` 与
        ASR 解码互斥（llama.cpp 不保证并发安全）。
        """
        if not isinstance(text, str):
            raise ValueError("correct.text 必须是字符串")
        with self._load_lock:
            self._ensure_correction_locked()
            result = correction.correct(text)
        if result.applied:
            # 硬约束：日志只记长度，禁止出现文本
            _log(f"correct 采纳 fixes={result.fixes} in={len(text)} out={len(result.text)} ms={result.ms}")
        else:
            _log(f"correct 未采纳 reason={result.reason} ms={result.ms}")
        return {
            "applied": result.applied,
            "text": result.text,
            "fixes": result.fixes,
            "reason": result.reason,
            "ms": result.ms,
        }

    def session_record(
        self,
        *,
        outcome: str = "Failed",
        chars: int = 0,
        e2e_ms: int = 0,
        ms_asr: int = 0,
        ms_post: int = 0,
        ms_inject: int = 0,
        speech_ms: int = 0,
        strategy: str = "",
        fixes: int = 0,
        error: str = "",
        text: str | None = None,
        raw_text: str | None = None,
        clean_text: str | None = None,
    ) -> dict:
        """`session_record` → {id}：把一次会话写进历史库（mode=ptt）。

        参数由 Rust 壳在注入后传（Trace 字段的壳侧子集）；文本三级对照
        原样交给 `add_history`——`store_text` 关闭时只落长度不落文本，
        该开关在 store 内生效，这里不复制判断逻辑。数据库问题绝不抛出：
        记账失败只返回 id=-1，绝不影响已完成的注入。
        """
        if self._store is None:
            return {"id": -1, "error": "no-store"}
        try:
            trace = Trace(
                outcome=Outcome(outcome) if outcome in Outcome._value2member_map_ else Outcome.FAILED,
                chars=_as_int(chars, "chars"),
                e2e_ms=_as_int(e2e_ms, "e2e_ms"),
                ms_asr=_as_int(ms_asr, "ms_asr"),
                ms_post=_as_int(ms_post, "ms_post"),
                ms_inject=_as_int(ms_inject, "ms_inject"),
                speech_ms=_as_int(speech_ms, "speech_ms"),
                injection_strategy=str(strategy),
                fixes=_as_int(fixes, "fixes"),
                error=str(error)[:120],
            )
            rid = self._store.add_history(
                trace, text, mode="ptt", raw_text=raw_text, clean_text=clean_text
            )
            _log(f"session_record id={rid} outcome={trace.outcome.value} chars={trace.chars}")
            return {"id": rid}
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响注入结果
            _log(f"⚠ 写历史失败：{type(exc).__name__}: {exc}")
            return {"id": -1}

    # ------------------------------------------------------------ 内部

    def _clear(self) -> None:
        self._buf.clear()
        self._frames = 0
        self._overflow_warned = False

    def _take(self) -> np.ndarray:
        """取出并清空缓冲。空缓冲返回空数组（调用方按"空串"处理）。"""
        if not self._buf:
            self._clear()
            return np.zeros(0, dtype=np.int16)
        pcm = np.concatenate(self._buf).astype(np.int16, copy=False)
        self._clear()
        return pcm
