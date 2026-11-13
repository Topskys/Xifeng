"""D16 推理运行时：IPC 侧的「缓冲 + 裁剪 + ASR 解码」。

Rust 壳接管采集/注入/后处理后（决策 D16，docs/00-process/plan.md），
Python 核心只暴露四个推理方法（契约见 docs/03-core-concepts/ipc-contract.md）：

    asr_begin   清空缓冲，确保模型已加载（首次调用会加载，2~4s，调用方容忍 30s）
    asr_feed    追加 16kHz 单声道 i16 LE 裸字节（base64），返回累计帧数
    asr_end     静音裁剪/VAD + ASR 解码，返回原始文本（未经 postprocess）
    asr_cancel  丢弃缓冲（Esc 取消本次）

设计要点：
- 模型延迟加载 + 常驻：`serve()` 发出 ready 后由后台线程跑 `warmup()`，
  绝不阻塞协议主循环；首次 `asr_begin` 一般已不用等冷启动（G8）
- 缓冲只在 IPC 主循环（单线程顺序处理）里访问，无需加锁；唯一的并发点是
  模型加载/解码 vs warmup 线程，统一用 `_load_lock` 串行化
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

import numpy as np

from . import asr, vad as vadlib
from .pipeline import prepare_pcm

SAMPLE_RATE = 16000
MAX_SECONDS = 60
#: 缓冲上限（帧 = i16 采样数）：60s ≈ 960000 帧，超出丢弃新数据
MAX_FRAMES = MAX_SECONDS * SAMPLE_RATE


def _log(msg: str) -> None:
    """日志只走 stderr —— stdout 是 IPC 协议专用通道。"""
    try:
        print(f"[asr-runtime] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - stderr 不可写时也不能影响协议
        pass


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

        self._buf: list[np.ndarray] = []
        self._frames = 0
        self._overflow_warned = False
        self._load_lock = threading.Lock()
        self._loaded = False
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
            _log(f"warmup 完成 backend={asr.backend()}")
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
