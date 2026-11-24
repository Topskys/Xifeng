"""D16 运行时方法的 IPC 回归（asr_begin/feed/end/cancel + correct/session_record）。

契约见 docs/03-core-concepts/ipc-contract.md「D16 运行时方法」。
真实模型太重，这里 monkeypatch 掉 asr.load / asr.transcribe 与 VAD/裁剪，
只验证协议语义、缓冲行为与失败隔离；算法本身由 test_vad / test_postprocess 等守。
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from xifeng import asr, correction, ipc, vad
from xifeng.asr import AsrError
from xifeng.asr_runtime import AsrRuntime
from xifeng.store import Store

SPIKE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "ipc-asr.db")
    yield s
    s.close()


def _run(store: Store, runtime: AsrRuntime | None, *messages: object) -> list[dict]:
    lines = "".join(
        (m if isinstance(m, str) else json.dumps(m, ensure_ascii=False)) + "\n"
        for m in messages
    )
    out = io.StringIO()
    rc = ipc.serve(store, stdin=io.StringIO(lines), stdout=out, runtime=runtime)
    assert rc == 0
    return [json.loads(l) for l in out.getvalue().splitlines()]


def _b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode()


def _pcm_bytes(seconds: float = 1.0) -> bytes:
    """已知正弦 PCM（16kHz i16 LE）：幅值足够大，不会被误判成静音。"""
    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * 440 * t) * 3000).astype("<i2").tobytes()


@pytest.fixture
def fake_asr(monkeypatch: pytest.MonkeyPatch):
    """把模型加载/解码/裁剪全部换成可控桩，记录调用入参。"""
    calls: dict[str, object] = {"load": 0, "transcribe": 0, "pcm_frames": []}

    def fake_load(**_kw: object) -> None:
        calls["load"] += 1

    def fake_transcribe(pcm: bytes, sample_rate: int = 16000) -> dict:
        calls["transcribe"] += 1
        calls["pcm_frames"].append(len(pcm) // 2)
        calls["sample_rate"] = sample_rate
        # 故意带语气词：postprocess 会清理它，end 必须原样返回（未经后处理）
        return {"text": "呃 今天 我们 嗯 开个会。", "lang": "zh", "rtf": 0.0,
                "ms": 1, "audio_s": round(len(pcm) / 2 / sample_rate, 2)}

    monkeypatch.setattr(asr, "load", fake_load)
    monkeypatch.setattr(asr, "transcribe", fake_transcribe)
    # 裁剪用恒等桩：这里不测算法，只保证「喂进去的帧 = 解码收到的帧」
    monkeypatch.setattr(vad, "trim",
                        lambda pcm, cfg=None, sample_rate=16000: (np.asarray(pcm, dtype=np.int16), 0))
    return calls


# ---------------------------------------------------------------- 正常链路


def test_begin_feed_end_roundtrip(store: Store, fake_asr: dict) -> None:
    rt = AsrRuntime(autostart_warmup=False)
    pcm = _pcm_bytes(1.0)
    _ready, begin, feed, end = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(pcm)}},
        {"id": 3, "method": "asr_end", "params": {}},
    )
    assert begin == {"id": 1, "ok": True, "result": {}}
    assert feed["ok"] is True and feed["result"] == {"frames": 16000}
    assert end["ok"] is True
    result = end["result"]
    # 原始输出未经 postprocess：语气词必须原样保留
    assert result["text"] == "呃 今天 我们 嗯 开个会。"
    assert result["chars"] == len(result["text"])
    assert isinstance(result["ms"], int) and result["ms"] >= 0
    # 解码收到的是喂进去的全部帧，采样率 16k
    assert fake_asr["transcribe"] == 1
    assert fake_asr["pcm_frames"] == [16000]
    assert fake_asr["sample_rate"] == 16000


def test_end_clears_buffer(store: Store, fake_asr: dict) -> None:
    rt = AsrRuntime(autostart_warmup=False)
    _ready, _begin, _feed, end1, end2 = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(_pcm_bytes(0.5))}},
        {"id": 3, "method": "asr_end", "params": {}},
        {"id": 4, "method": "asr_end", "params": {}},
    )
    assert end1["result"]["text"] != ""
    assert end2["result"] == {"text": "", "chars": 0, "ms": 0}   # 缓冲已清空
    assert fake_asr["transcribe"] == 1                          # 第二次没再解码


def test_empty_end_is_legal(store: Store, fake_asr: dict) -> None:
    """begin 之后什么都没喂：空串是合法结果，不解码。"""
    rt = AsrRuntime(autostart_warmup=False)
    _ready, _begin, end = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_end", "params": {}},
    )
    assert end["result"] == {"text": "", "chars": 0, "ms": 0}
    assert fake_asr["transcribe"] == 0


# ---------------------------------------------------------------- cancel


def test_cancel_discards_buffer(store: Store, fake_asr: dict) -> None:
    rt = AsrRuntime(autostart_warmup=False)
    _ready, _begin, _feed, cancel, end = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(_pcm_bytes(0.5))}},
        {"id": 3, "method": "asr_cancel", "params": {}},
        {"id": 4, "method": "asr_end", "params": {}},
    )
    assert cancel == {"id": 3, "ok": True, "result": {}}
    assert end["result"] == {"text": "", "chars": 0, "ms": 0}
    assert fake_asr["transcribe"] == 0


def test_begin_also_resets_buffer(store: Store, fake_asr: dict) -> None:
    """cancel 忘了发、直接开始下一句：begin 自己也必须清缓冲。"""
    rt = AsrRuntime(autostart_warmup=False)
    _ready, _b1, _f1, _e1, _b2, _f2, end2 = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(_pcm_bytes(0.5))}},
        {"id": 3, "method": "asr_end", "params": {}},
        {"id": 4, "method": "asr_begin", "params": {}},
        {"id": 5, "method": "asr_feed", "params": {"pcm_b64": _b64(_pcm_bytes(0.5))}},
        {"id": 6, "method": "asr_end", "params": {}},
    )
    assert end2["ok"] is True
    assert fake_asr["pcm_frames"] == [8000, 8000]   # 第二句不带第一句的残留


# ---------------------------------------------------------------- 上限


def test_feed_over_limit_drops_new_data(
    store: Store, fake_asr: dict, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """缓冲超过上限：丢弃新数据、累计帧数不变，只警告一次。"""
    from xifeng import asr_runtime

    monkeypatch.setattr(asr_runtime, "MAX_FRAMES", 1000)
    rt = AsrRuntime(autostart_warmup=False)
    chunk = _pcm_bytes(600 / 16000)          # 600 帧/次
    _ready, _begin, f1, f2, f3, end = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(chunk)}},
        {"id": 3, "method": "asr_feed", "params": {"pcm_b64": _b64(chunk)}},   # 600+600 > 1000 → 丢
        {"id": 4, "method": "asr_feed", "params": {"pcm_b64": _b64(chunk)}},   # 再丢，不再警告
        {"id": 5, "method": "asr_end", "params": {}},
    )
    assert f1["result"] == {"frames": 600}
    assert f2["result"] == {"frames": 600}
    assert f3["result"] == {"frames": 600}
    assert fake_asr["pcm_frames"] == [600]          # 解码只见到缓冲里的 600 帧
    err = capsys.readouterr().err
    assert err.count("上限") == 1                    # 只警告一次


def test_feed_60s_boundary_is_accepted(store: Store, fake_asr: dict) -> None:
    """真实上限 960000 帧：正好 60s 能全收，再多一帧才丢。"""
    rt = AsrRuntime(autostart_warmup=False)
    full = _pcm_bytes(60.0)                          # 恰好 960000 帧
    one = _pcm_bytes(1 / 16000)                      # 1 帧
    _ready, _begin, f1, f2 = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(full)}},
        {"id": 3, "method": "asr_feed", "params": {"pcm_b64": _b64(one)}},
    )
    assert f1["result"] == {"frames": 960000}
    assert f2["result"] == {"frames": 960000}


# ---------------------------------------------------------------- 错误隔离


def test_asr_load_failure_returns_error_and_survives(
    store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型加载失败（AsrError）→ ok=false + 简短 error，进程继续活着。"""
    def boom(**_kw: object) -> None:
        raise AsrError("模型文件不存在: models/asr/model.onnx")

    monkeypatch.setattr(asr, "load", boom)
    rt = AsrRuntime(autostart_warmup=False)
    _ready, begin, cancel, begin2 = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_cancel", "params": {}},
        {"id": 3, "method": "asr_begin", "params": {}},
    )
    assert begin["ok"] is False
    assert "模型" in begin["error"]
    assert cancel["ok"] is True               # 单条失败隔离：下一条照常处理
    assert begin2["ok"] is False              # 仍然失败，但没崩
    assert begin2["id"] == 3


def test_transcribe_failure_returns_error_and_survives(
    store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解码阶段抛 AsrError 同样只影响这一条请求。"""
    def boom(pcm: bytes, sample_rate: int = 16000) -> dict:
        raise AsrError("推理失败: 模拟崩溃")

    monkeypatch.setattr(asr, "load", lambda **_kw: None)
    monkeypatch.setattr(asr, "transcribe", boom)
    rt = AsrRuntime(autostart_warmup=False)
    _ready, _begin, _feed, end, stats = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(_pcm_bytes(0.3))}},
        {"id": 3, "method": "asr_end", "params": {}},
        {"id": 4, "method": "stats", "params": {"period": "all"}},
    )
    assert end["ok"] is False and "推理失败" in end["error"]
    assert stats["ok"] is True                # 进程还活着


def test_feed_bad_base64_and_odd_bytes(store: Store, fake_asr: dict) -> None:
    rt = AsrRuntime(autostart_warmup=False)
    _ready, not_b64, odd, good = _run(
        store, rt,
        {"id": 1, "method": "asr_feed", "params": {"pcm_b64": "!!不是base64!!"}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(b"\x01\x02\x03")}},  # 奇数字节
        {"id": 3, "method": "asr_feed", "params": {"pcm_b64": _b64(b"\x01\x02")}},
    )
    assert not_b64["ok"] is False and "base64" in not_b64["error"]
    assert odd["ok"] is False and "偶数" in odd["error"]
    assert good["ok"] is True and good["result"] == {"frames": 1}


def test_runtime_methods_reject_unknown_params(store: Store, fake_asr: dict) -> None:
    """多余参数显式报错（与 HTTP 对应方法同一策略）。"""
    rt = AsrRuntime(autostart_warmup=False)
    _ready, bad, good = _run(
        store, rt,
        {"id": 1, "method": "asr_begin", "params": {"nope": 1}},
        {"id": 2, "method": "asr_begin", "params": {}},
    )
    assert bad["ok"] is False and "不接受参数" in bad["error"]
    assert good["ok"] is True


def test_default_runtime_lazily_loads(store: Store, fake_asr: dict) -> None:
    """serve 不传 runtime：兜底路径也能跑完整链（懒加载，不起 warmup 线程）。"""
    _ready, begin, feed, end = _run(
        store, None,
        {"id": 1, "method": "asr_begin", "params": {}},
        {"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(_pcm_bytes(0.5))}},
        {"id": 3, "method": "asr_end", "params": {}},
    )
    assert begin["ok"] is True
    assert feed["result"] == {"frames": 8000}
    assert end["result"]["text"] == "呃 今天 我们 嗯 开个会。"
    assert fake_asr["load"] == 1


# ---------------------------------------------------------------- correct / session_record（P2）


def test_correct_unavailable_returns_original(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """纠错模型缺失：ok=true + applied=false + text=原文（不可用不是协议错误）。"""
    monkeypatch.setattr(correction, "_llm", None)
    monkeypatch.setattr(correction, "load", lambda **_kw: False)
    rt = AsrRuntime(autostart_warmup=False, model_correct=True)
    _ready, resp = _run(
        store, rt,
        {"id": 1, "method": "correct", "params": {"text": "今天下午三电开会把方案发给我"}},
    )
    assert resp["ok"] is True
    r = resp["result"]
    assert r["applied"] is False
    assert r["text"] == "今天下午三电开会把方案发给我"   # 守卫拒绝 = 原文
    assert r["reason"] == "unavailable"
    assert isinstance(r["ms"], int)


def test_correct_load_failure_is_sticky(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """加载失败是粘性的：第二次 correct 不再逐句重试（pipeline 同款语义）。"""
    calls = {"load": 0}

    def fake_load(**_kw: object) -> bool:
        calls["load"] += 1
        return False

    monkeypatch.setattr(correction, "_llm", None)
    monkeypatch.setattr(correction, "load", fake_load)
    rt = AsrRuntime(autostart_warmup=False, model_correct=True)
    _ready, r1, r2 = _run(
        store, rt,
        {"id": 1, "method": "correct", "params": {"text": "第一句测试文本"}},
        {"id": 2, "method": "correct", "params": {"text": "第二句测试文本"}},
    )
    assert r1["ok"] is True and r2["ok"] is True
    assert calls["load"] == 1


def test_correct_applied_returns_fixed_text(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """守卫放行：applied=true + 修正文本 + fixes；日志只记长度，不落文本。"""
    from xifeng.correction import CorrectionResult

    monkeypatch.setattr(correction, "load", lambda **_kw: True)
    monkeypatch.setattr(correction, "warmup", lambda: 3)
    monkeypatch.setattr(
        correction, "correct",
        lambda text, **_kw: CorrectionResult(text="今天下午三点开会", applied=True, reason="ok", fixes=2, ms=40),
    )
    rt = AsrRuntime(autostart_warmup=False, model_correct=True)
    _ready, resp = _run(
        store, rt,
        {"id": 1, "method": "correct", "params": {"text": "今天下午三电开会"}},
    )
    assert resp["ok"] is True
    r = resp["result"]
    assert r == {"applied": True, "text": "今天下午三点开会", "fixes": 2, "reason": "ok", "ms": 40}
    err = capsys.readouterr().err
    assert "今天" not in err                       # 红线：日志禁止出现识别文本
    assert "fixes=2" in err and "in=8 out=8" in err


def test_correct_uses_recent_history_and_custom_prompt(
    store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xifeng.trace import Outcome, Trace

    for i in range(12):
        store.add_history(Trace(outcome=Outcome.INJECTED), f"历史第{i + 1}轮")
    store.add_history(Trace(outcome=Outcome.FAILED), "失败历史不应读取")
    seen: dict[str, object] = {}

    def fake_correct(text: str, **kwargs: object):
        seen.update(kwargs)
        return CorrectionResult(text=text, reason="same")

    from xifeng.correction import CorrectionResult

    monkeypatch.setattr(correction, "load", lambda **_kw: True)
    monkeypatch.setattr(correction, "warmup", lambda: 1)
    monkeypatch.setattr(correction, "correct", fake_correct)
    rt = AsrRuntime(
        autostart_warmup=False,
        model_correct=True,
        store=store,
        correction_history_limit=10,
        correction_prompt="术语统一为 Xifeng",
    )
    _ready, resp = _run(store, rt, {"id": 1, "method": "correct", "params": {"text": "当前句"}})
    assert resp["ok"] is True
    assert seen["history"] == [f"历史第{i}轮" for i in range(3, 13)]
    assert seen["custom_prompt"] == "术语统一为 Xifeng"


def test_correct_ignores_history_when_store_text_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xifeng.correction import CorrectionResult
    from xifeng.trace import Outcome, Trace

    s = Store(tmp_path / "private.db", store_text=False)
    try:
        s.add_history(Trace(outcome=Outcome.INJECTED), "不应进入提示词")
        seen: dict[str, object] = {}
        monkeypatch.setattr(correction, "load", lambda **_kw: True)
        monkeypatch.setattr(correction, "warmup", lambda: 1)
        monkeypatch.setattr(
            correction, "correct",
            lambda text, **kw: seen.update(kw) or CorrectionResult(text=text, reason="same"),
        )
        rt = AsrRuntime(autostart_warmup=False, model_correct=True, store=s)
        _ready, resp = _run(s, rt, {"id": 1, "method": "correct", "params": {"text": "当前句"}})
        assert resp["ok"] is True and seen["history"] == []
    finally:
        s.close()


def test_correct_rejects_non_string(store: Store, fake_asr: dict) -> None:
    rt = AsrRuntime(autostart_warmup=False)
    _ready, resp = _run(store, rt, {"id": 1, "method": "correct", "params": {"text": 5}})
    assert resp["ok"] is False and "字符串" in resp["error"]


def test_warmup_preheats_correction(
    store: Store, fake_asr: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """model_correct=true 时 warmup 预热 LLM：加载+热身各一次，日志说明可用。"""
    calls = {"load": 0, "warmup": 0}

    def fake_load(**_kw: object) -> bool:
        calls["load"] += 1
        return True

    def fake_warmup() -> int:
        calls["warmup"] += 1
        return 7

    monkeypatch.setattr(correction, "_llm", None)
    monkeypatch.setattr(correction, "load", fake_load)
    monkeypatch.setattr(correction, "warmup", fake_warmup)
    rt = AsrRuntime(autostart_warmup=False, model_correct=True)
    rt.warmup()
    assert calls == {"load": 1, "warmup": 1}
    assert "correct 模型已热身" in capsys.readouterr().err


def test_session_record_writes_history(store: Store) -> None:
    """记账写入 history 表（mode=ptt），返回自增 id。"""
    rt = AsrRuntime(autostart_warmup=False, store=store)
    _ready, resp = _run(
        store, rt,
        {"id": 1, "method": "session_record", "params": {
            "outcome": "Injected", "chars": 5, "e2e_ms": 800,
            "ms_asr": 500, "ms_post": 12, "ms_inject": 30, "speech_ms": 1200,
            "strategy": "unicode", "fixes": 2,
            "text": "你好世界。", "raw_text": "呃 你好世界", "clean_text": "你好世界",
        }},
    )
    assert resp["ok"] is True
    assert resp["result"]["id"] == 1
    row = store.recent(1)[0]
    assert row["outcome"] == "Injected" and row["chars"] == 5 and row["fixes"] == 2
    assert row["e2e_ms"] == 800 and row["ms_asr"] == 500 and row["ms_inject"] == 30
    assert row["speech_ms"] == 1200 and row["strategy"] == "unicode"
    assert row["text"] == "你好世界。"               # store_text 默认开：文本落库
    assert row["raw_text"] == "呃 你好世界" and row["clean_text"] == "你好世界"


def test_session_record_respects_store_text_off(tmp_path: Path) -> None:
    """store_text 关闭：只落长度，不落任何一级文本（仓库硬约束）。"""
    s = Store(tmp_path / "off.db", store_text=False)
    try:
        rt = AsrRuntime(autostart_warmup=False, store=s)
        _ready, resp = _run(
            s, rt,
            {"id": 1, "method": "session_record", "params": {
                "outcome": "Injected", "chars": 5, "text": "你好世界。", "raw_text": "呃",
            }},
        )
        assert resp["result"]["id"] == 1
        row = s.recent(1)[0]
        assert row["chars"] == 5
        assert row["text"] is None and row["raw_text"] is None and row["clean_text"] is None
    finally:
        s.close()


def test_session_record_failures_never_raise(store: Store) -> None:
    """无 store / 坏参数 / 未知 outcome：恒 ok=true，绝不影响已完成的注入。"""
    rt_no_store = AsrRuntime(autostart_warmup=False)
    rt = AsrRuntime(autostart_warmup=False, store=store)
    _ready, no_store = _run(
        store, rt_no_store,
        {"id": 2, "method": "session_record", "params": {"outcome": "Injected"}},
    )
    _ready2, bad_param, bad_outcome = _run(
        store, rt,
        {"id": 3, "method": "session_record", "params": {"chars": "不是数字"}},
        {"id": 4, "method": "session_record", "params": {"outcome": "奇怪的状态"}},
    )
    assert no_store == {"id": 2, "ok": True, "result": {"id": -1, "error": "no-store"}}
    assert bad_param["ok"] is True and bad_param["result"]["id"] == -1
    assert bad_outcome["ok"] is True
    row = store.recent(1)[0]
    assert row["outcome"] == "Failed"               # 未知状态兜底为 Failed，不写脏数据


def test_session_record_rejects_unknown_params(store: Store) -> None:
    rt = AsrRuntime(autostart_warmup=False, store=store)
    _ready, resp = _run(
        store, rt,
        {"id": 1, "method": "session_record", "params": {"nope": 1}},
    )
    assert resp["ok"] is False and "不接受参数" in resp["error"]


# ---------------------------------------------------------------- stdout 红线


def test_asr_feed_roundtrip_cost_budget(tmp_path: Path) -> None:
    """asr_feed IPC 往返成本（真实子进程管道，逐条计时；G3 预算佐证）。

    壳按 40ms 窗口攒批喂送：每批往返必须远小于窗口，否则追不上实时。
    断言放宽到均值 <10ms——只抓回归（如 base64/JSON 路径劣化），不测极限。
    真实数字打印出来留档（p50/p95）。
    """
    from time import perf_counter

    n_batches = 20
    batch = _pcm_bytes(0.04)  # 40ms @16k = 640 帧，与壳的窗口一致

    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "run_spike.py", "--ipc",
         "--db", str(tmp_path / "rtt.db")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,   # 防 stderr 管道填满死锁
        text=True, encoding="utf-8", cwd=str(SPIKE_DIR),
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        first = proc.stdout.readline()
        assert json.loads(first)["event"] == "ready"

        def ask(msg: dict) -> dict:
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        ask({"id": 0, "method": "asr_begin", "params": {}})
        samples: list[float] = []
        for i in range(n_batches):
            t0 = perf_counter()
            resp = ask({"id": i + 1, "method": "asr_feed",
                        "params": {"pcm_b64": _b64(batch)}})
            samples.append((perf_counter() - t0) * 1000)
            assert resp["ok"] is True
            assert resp["result"]["frames"] == 640 * (i + 1)
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass
        proc.wait(timeout=30)

    samples.sort()
    avg = sum(samples) / len(samples)
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95) - 1]
    print(f"\nasr_feed 往返（{n_batches} 批 × 640 帧）：avg={avg:.2f}ms "
          f"p50={p50:.2f}ms p95={p95:.2f}ms")
    assert avg < 10.0, f"asr_feed 平均往返 {avg:.2f}ms > 10ms 预算"


def test_asr_stream_stdout_is_pure_json(
    store: Store, fake_asr: dict, capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整个 asr_* 会话期间（含错误日志），stdout 必须仍然全是协议 JSON。"""
    rt = AsrRuntime(autostart_warmup=False)
    lines = "\n".join(
        [
            json.dumps({"id": 1, "method": "asr_begin", "params": {}}),
            json.dumps({"id": 2, "method": "asr_feed", "params": {"pcm_b64": _b64(_pcm_bytes(0.5))}}),
            json.dumps({"id": 3, "method": "asr_end", "params": {}}),
            json.dumps({"id": 4, "method": "asr_cancel", "params": {}}),
        ]
    ) + "\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    assert ipc.serve(store, runtime=rt) == 0

    captured = capfd.readouterr()
    msgs = [json.loads(l) for l in captured.out.splitlines()]   # 混入日志会在这里炸
    assert [m.get("id") for m in msgs[1:]] == [1, 2, 3, 4]
    # 识别文本只允许出现在协议响应里，日志（stderr）只许出现长度
    assert "呃 今天" not in captured.err
    assert "chars=" in captured.err


# ---------------------------------------------------------------- 设置热更（P2-②）


def test_apply_settings_flags_do_not_reload(fake_asr: dict) -> None:
    """非构造参数（切分/裁剪/纠错）只改开关，绝不为它们付几秒的重载。"""
    rt = AsrRuntime(autostart_warmup=False)
    rt.ensure_loaded()
    assert fake_asr["load"] == 1

    res = rt.apply_settings(use_vad=True, trim_silence=False, model_correct=True)
    assert res == {
        "reloaded": False,
        "language": rt.language,
        "threads": rt.threads,
        "use_itn": rt.use_itn,
    }
    assert (rt.use_vad, rt.trim_silence, rt.model_correct) == (True, False, True)
    assert fake_asr["load"] == 1


def test_apply_settings_use_itn_reloads_when_changed(fake_asr: dict) -> None:
    """ITN 是 recognizer 构造参数：改了必须重建，没改不能白付重载。"""
    loads: list[object] = []

    def fake_load(**kw: object) -> None:
        loads.append(kw.get("use_itn"))

    import xifeng.asr as asr_mod

    orig_load = asr_mod.load
    asr_mod.load = fake_load
    try:
        rt = AsrRuntime(autostart_warmup=False, use_itn=True)
        rt.ensure_loaded()
        assert loads == [True]

        res = rt.apply_settings(use_itn=False)
        assert res["reloaded"] is True and res["use_itn"] is False
        assert rt.use_itn is False
        assert loads == [True, False]

        res = rt.apply_settings(use_itn=False)
        assert res["reloaded"] is False
        assert loads == [True, False]
    finally:
        asr_mod.load = orig_load


def test_apply_settings_threads_reloads_when_loaded(fake_asr: dict) -> None:
    """线程数是 recognizer 构造参数：已加载时改了必须重建。"""
    rt = AsrRuntime(autostart_warmup=False, threads=4)
    rt.ensure_loaded()
    assert fake_asr["load"] == 1

    res = rt.apply_settings(threads=8)
    assert res["reloaded"] is True and res["threads"] == 8
    assert rt.threads == 8
    assert fake_asr["load"] == 2


def test_apply_settings_lazy_path_skips_reload(fake_asr: dict) -> None:
    """还没加载过：改属性就够，首次 load 自然用新值（不提前加载）。"""
    rt = AsrRuntime(autostart_warmup=False, threads=4)
    res = rt.apply_settings(threads=8, language="en")
    assert res["reloaded"] is False
    assert fake_asr["load"] == 0
    rt.ensure_loaded()
    assert fake_asr["load"] == 1


def test_apply_settings_rejects_bad_language(fake_asr: dict) -> None:
    """非法语言显式报错（与 set_language 同口径），且不留下半截状态。"""
    rt = AsrRuntime(autostart_warmup=False, language="zh")
    with pytest.raises(ValueError, match="不支持的识别语言"):
        rt.apply_settings(language="klingon")
    assert rt.language == "zh"


def test_settings_save_over_ipc_syncs_flags_and_threads(store: Store, fake_asr: dict) -> None:
    """协议层：保存线程数 → 运行时重建；保存裁剪开关 → 只改属性。"""
    rt = AsrRuntime(autostart_warmup=False, threads=4)
    rt.ensure_loaded()
    assert fake_asr["load"] == 1

    _ready, res = _run(
        store, rt,
        {"id": 1, "method": "settings_save", "params": {"threads": 8, "trim_silence": False}},
    )
    assert res["ok"] is True
    assert rt.threads == 8 and rt.trim_silence is False
    assert fake_asr["load"] == 2          # 线程数变了：重建一次

    _ready, res = _run(
        store, rt, {"id": 2, "method": "settings_save", "params": {"trim_silence": True}}
    )
    assert res["ok"] is True
    assert rt.trim_silence is True
    assert fake_asr["load"] == 2          # 只改开关：不再重建


def test_settings_save_failure_keeps_saved_config(store: Store, fake_asr: dict, monkeypatch) -> None:
    """重建失败（模型坏了）不能让保存失败：设置已落 JSON，新值保留待下次加载。"""
    import xifeng.asr as asr_mod
    from xifeng import settings as user_settings

    rt = AsrRuntime(autostart_warmup=False, threads=4)
    rt.ensure_loaded()                       # 先用好桩加载，让重建路径真的会跑

    def boom(**_kw: object) -> None:
        raise asr_mod.AsrError("模型坏了")

    monkeypatch.setattr(asr_mod, "load", boom)
    _ready, res = _run(
        store, rt, {"id": 1, "method": "settings_save", "params": {"threads": 8}}
    )
    assert res["ok"] is True              # 保存本身成功
    assert rt.threads == 8                # 新值保留，下次加载重试
    assert rt._loaded is False            # 重建失败：标记回未加载
    assert user_settings.load(None).threads == 8


def test_models_use_over_ipc_reloads_model(fake_asr: dict, store: Store, tmp_path) -> None:
    """切换模型目录要重建 recognizer，否则「已切换」等于没切。"""
    good = tmp_path / "model"
    good.mkdir()
    (good / "model.onnx").write_bytes(b"x")
    (good / "tokens.txt").write_text("a\n", encoding="utf-8")

    rt = AsrRuntime(autostart_warmup=False)
    rt.ensure_loaded()
    assert fake_asr["load"] == 1

    _ready, res = _run(
        store, rt, {"id": 1, "method": "models_use", "params": {"path": str(good)}}
    )
    assert res["ok"] is True
    assert rt.model_dir == str(good)
    assert fake_asr["load"] == 2


# ---------------------------------------------------------------- 语言切换


def test_set_language_switches_and_reloads_model(store: Store, fake_asr: dict) -> None:
    """换语言必须**重建 recognizer**（language 是 sherpa 的构造参数）。"""
    loads: list[str] = []

    def fake_load(**kw: object) -> None:
        loads.append(str(kw.get("language")))

    fake_asr.clear()
    import xifeng.asr as asr_mod

    orig_load = asr_mod.load
    asr_mod.load = fake_load
    try:
        rt = AsrRuntime(autostart_warmup=False, language="zh")
        rt.ensure_loaded()
        assert loads == ["zh"]

        rt.set_language("en")
        assert loads == ["zh", "en"]
        assert rt.language == "en"
    finally:
        asr_mod.load = orig_load


def test_set_language_same_value_does_not_reload(store: Store, fake_asr: dict) -> None:
    """语言没变就不该重载模型（重载要几秒，不能白付）。"""
    loads: list[str] = []

    def fake_load(**kw: object) -> None:
        loads.append(str(kw.get("language")))

    import xifeng.asr as asr_mod

    orig_load = asr_mod.load
    asr_mod.load = fake_load
    try:
        rt = AsrRuntime(autostart_warmup=False, language="zh")
        rt.ensure_loaded()
        rt.set_language("zh")
        assert loads == ["zh"]
    finally:
        asr_mod.load = orig_load


def test_set_language_rejects_unsupported(store: Store) -> None:
    """非法语言显式报错，不静默忽略（用户点了没反应是最糟的体验）。"""
    rt = AsrRuntime(autostart_warmup=False)
    with pytest.raises(ValueError, match="不支持的识别语言"):
        rt.set_language("klingon")
    with pytest.raises(ValueError):
        rt.set_language("")


def test_set_language_over_ipc(store: Store, fake_asr: dict) -> None:
    """协议层：set_language 走 IPC，返回新语言。"""
    rt = AsrRuntime(autostart_warmup=False, language="zh")
    _ready, res = _run(
        store, rt,
        {"id": 1, "method": "set_language", "params": {"language": "ja"}},
    )
    assert res["ok"] is True
    assert res["result"] == {"language": "ja"}


def test_settings_save_with_new_language_syncs_runtime(store: Store, fake_asr: dict) -> None:
    """保存设置带新语言 → 运行时自动切（用户不必手动调 set_language）。"""
    import xifeng.asr as asr_mod

    loads: list[str] = []

    def fake_load(**kw: object) -> None:
        loads.append(str(kw.get("language")))

    orig_load = asr_mod.load
    asr_mod.load = fake_load
    try:
        rt = AsrRuntime(autostart_warmup=False, language="zh")
        rt.ensure_loaded()
        _ready, res = _run(
            store, rt,
            {"id": 1, "method": "settings_save", "params": {"language": "en"}},
        )
        assert res["ok"] is True
        assert res["result"]["language"] == "en"
        assert rt.language == "en"
        assert loads == ["zh", "en"]
    finally:
        asr_mod.load = orig_load


# ---------------------------------------------------------------- 流式（句内分段）

def test_stream_begin_feed_end(store: Store, fake_asr: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """流式：begin → feed → end 能下发分段，索引从 0 递增。"""
    # VAD 段切分桩：整个缓冲作为一个段；end == buf.size 会被判为未闭合而保留，
    # 所以 feed 不产生段，end 时作为尾段转写产生 0 号片段。
    import xifeng.vad as vad_mod
    monkeypatch.setattr(
        vad_mod,
        "segment",
        lambda buf, cfg: ([(0, len(buf))] if len(buf) else []),
    )
    rt = AsrRuntime(autostart_warmup=False, use_vad=True)
    pcm = _pcm_bytes(1.0)
    ready, begin, feed, end = _run(
        store, rt,
        {"id": 1, "method": "asr_stream_begin", "params": {}},
        {"id": 2, "method": "asr_stream_feed", "params": {"pcm_b64": _b64(pcm)}},
        {"id": 3, "method": "asr_stream_end", "params": {}},
    )
    assert begin["ok"] is True and begin["result"] == {}
    assert feed["ok"] is True and feed["result"]["frames"] == 16000
    assert end["ok"] is True
    segs = end["result"]["segments"]
    assert len(segs) == 1
    assert segs[0]["index"] == 0
    assert segs[0]["chars"] == len(segs[0]["text"])
    assert fake_asr["transcribe"] == 1


def test_stream_cancel_clears_state(store: Store, fake_asr: dict) -> None:
    """stream_cancel 清空缓冲并关闭会话。"""
    rt = AsrRuntime(autostart_warmup=False)
    pcm = _pcm_bytes(0.5)
    ready, begin, feed, cancel = _run(
        store, rt,
        {"id": 1, "method": "asr_stream_begin", "params": {}},
        {"id": 2, "method": "asr_stream_feed", "params": {"pcm_b64": _b64(pcm)}},
        {"id": 3, "method": "asr_stream_cancel", "params": {}},
    )
    assert cancel["ok"] is True and cancel["result"] == {}
    assert rt._stream_active is False
    assert rt._stream_frames == 0

