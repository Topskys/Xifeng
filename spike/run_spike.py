"""手动跑一次：把一段音频走完 录音→ASR→清理→注入，并打印 Trace。

用法：
    uv run python run_spike.py --text "呃 今天 我们 嗯 开个会。" --no-inject
    uv run python run_spike.py --wav sample.wav
    uv run python run_spike.py --seconds 3
    uv run python run_spike.py --interactive
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from xifeng import (
    applog, asr, audio, autostart, correction, hotkey, inject as inject_mod, paths,
    postprocess, popup as popup_mod, winenv,
)
from xifeng import settings as user_settings
from xifeng.pipeline import Pipeline, PipelineConfig, record_interactive, record_seconds
from xifeng.store import Store, describe as describe_db
from xifeng.trace import TraceSink


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Xifeng M0 spike —— 跑通一次语音输入")
    src = p.add_mutually_exclusive_group()
    p.add_argument("--text", help="跳过 ASR，直接用这段文本走清理+注入（调试用）")
    src.add_argument("--wav", help="从 wav 文件读取音频（16-bit）")
    src.add_argument("--seconds", type=float, default=3.0, help="录制固定秒数（默认 3）")
    src.add_argument("--interactive", action="store_true", help="回车开始/停止录音")
    src.add_argument("--ptt", action="store_true", help="按住热键说话、松开结束（全局键，默认 Ctrl）")

    p.add_argument("--tray", action="store_true",
                   help="托盘常驻：隐藏控制台、无限次 PTT，菜单可开窗口/自启/退出（隐含 --ptt）")
    p.add_argument("--gui", action="store_true", help="打开主窗口（统计/模型/配置/历史）后退出")
    p.add_argument("--web", action="store_true", help="Web 主界面（Edge 应用模式窗口）")
    p.add_argument("--ipc", action="store_true",
                   help="stdio NDJSON 服务（Tauri 壳拉起）：不开 HTTP、不启托盘，日志全走 stderr")
    p.add_argument("--tab", default="stats",
                   choices=["stats", "models", "config", "history", "about"],
                   help="打开窗口时选中的页（默认 stats）")
    p.add_argument("--autostart", choices=["on", "off", "status"], default=None,
                   help="开机自启：on 开启 / off 关闭 / status 查看后退出")

    p.add_argument("--key", default=hotkey.DEFAULT_KEY, choices=sorted(hotkey.KEYS), help="PTT 键位")
    p.add_argument("--resident-mic", action="store_true",
                   help="常驻麦克风（更低延迟，但会一直占用设备；默认按下才开麦）")
    p.add_argument("--no-popup", action="store_true",
                   help="不显示录音指示浮窗（默认显示）")
    p.add_argument("--single-press", action="store_true",
                   help="旧触发方式：按住即录（默认双击后保持长按）")
    p.add_argument("--popup", action="store_true",
                   help="开启录音指示浮窗（默认显示）")
    p.add_argument("--times", type=int, default=None,
                   help="PTT 模式重复次数（0 = 一直等；--tray 时默认 0，其它默认 1）")
    p.add_argument("--min-hold-ms", type=int, default=hotkey.DEFAULT_MIN_HOLD_MS,
                   help="按住时长下限，低于此值按误触丢弃")
    p.add_argument("--max-seconds", type=float, default=hotkey.DEFAULT_MAX_SECONDS,
                   help="单次录音上限（秒），超出截断")
    p.add_argument("--diagnose-keys", type=float, nargs="?", const=15.0, default=None,
                   metavar="SECONDS", help="诊断：打印这段时间内按下的所有键位（默认 15s）")

    p.add_argument("--backend", default="auto", choices=["auto", "mock", "sherpa"])
    p.add_argument("--model-dir", default=None, help="SenseVoice 模型目录（默认 IME_MODELS_DIR 或 models/asr/...）")
    p.add_argument("--threads", type=int, default=8, help="ONNX 线程数（8 核实测 8 最快）")
    p.add_argument("--download-source", default=None, choices=user_settings.DOWNLOAD_SOURCES,
                   help="模型下载源（GUI 配置页同步，不填则用数据库里的值）")
    p.add_argument("--no-inject", action="store_true", help="只处理不注入（安全模式）")
    p.add_argument("--vad", action="store_true", help="启用能量 VAD（M0 默认关闭）")
    p.add_argument("--no-trim", dest="trim", action="store_false",
                   help="不裁首尾静音（默认裁；可减少长静音幻觉与 ASR 耗时）")
    p.set_defaults(trim=True)
    p.add_argument("--restore-focus", action="store_true", help="焦点被切走时尝试抢回")
    p.add_argument("--correct", action="store_true", default=None,
                   help="开启本地模型纠错（默认关；+0.5–1.5s/句，见 G32）")
    p.add_argument("--inject", choices=("unicode", "clipboard", "auto"), default="auto",
                   help="注入策略：unicode=只发按键；clipboard=剪贴板+Ctrl+V；"
                        "auto=先按键，确认没打进去才兜底（默认）")
    p.add_argument("--clipboard-restore-ms", type=int, default=500, metavar="MS",
                   help="剪贴板兜底后多久恢复原内容（默认 500ms；太快会粘出原内容）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出 Trace")
    p.add_argument("--trace", default=None, help="Trace JSONL 路径（默认不落盘）")
    p.add_argument("--print-raw", action="store_true", help="打印 ASR 原文（默认不打印，隐私约束）")
    p.add_argument("--speak-result", action="store_true", help="把清理后的文本打到 stdout")

    # --- SQLite：历史 / 统计
    p.add_argument("--db", default=None, help="数据库路径（默认 IME_DB 或 %%LOCALAPPDATA%%\\Xifeng\\xifeng.db）")
    p.add_argument("--no-db", action="store_true", help="不打开数据库（不记历史）")
    p.add_argument("--no-store-text", action="store_true", help="历史只记长度，不落识别文本")
    p.add_argument("--history", type=int, nargs="?", const=20, default=None, metavar="N",
                   help="打印最近 N 条历史后退出（默认 20）")
    p.add_argument("--stats", action="store_true", help="打印统计（次数/成功率/P50/P95）后退出")
    return p.parse_args(argv)


# ---------------------------------------------------------------- SQLite 子命令

def open_store(args: argparse.Namespace) -> Store | None:
    if args.no_db:
        return None
    try:
        return Store(args.db, store_text=not args.no_store_text)
    except Exception as exc:  # noqa: BLE001 - 数据库坏了不能拦住输入
        print(f"[db] ⚠ 打不开数据库（{exc}），本次不记历史", flush=True)
        return None


# 设置项 ↔ 命令行开关：命令行显式给了就优先，避免"GUI 一保存把命令行参数吃掉"
_SETTING_FLAGS = {
    "--inject": "inject_strategy",
    "--no-popup": "show_popup",
    "--restore-focus": "restore_focus",
    "--clipboard-restore-ms": "clipboard_restore_ms",
    "--no-trim": "trim_silence",
    "--trim": "trim_silence",
    "--vad": "use_vad",
    "--correct": "model_correct",
    "--key": "hotkey",
    "--model-dir": "model_dir",
    "--threads": "threads",
    "--download-source": "download_source",
}


def apply_user_settings(
    args: argparse.Namespace,
    store: Store | None,
    argv: list[str] | None = None,
) -> user_settings.Settings:
    """把 SQLite 里的用户设置填进 args 默认值。

    只覆盖"用户没在命令行上显式指定"的项 —— 排查问题时 `--inject unicode`
    必须能压过 GUI 里的选择。
    """
    cfg = user_settings.load(store)
    if store is None:
        return cfg
    tokens = list(sys.argv[1:] if argv is None else argv)
    given: set[str] = set()
    for token in tokens:
        key = _SETTING_FLAGS.get(token.split("=", 1)[0])
        if key:
            given.add(key)
    if "inject_strategy" not in given:
        args.inject = cfg.inject_strategy
    if "restore_focus" not in given:
        args.restore_focus = cfg.restore_focus
    if "clipboard_restore_ms" not in given:
        args.clipboard_restore_ms = cfg.clipboard_restore_ms
    if "trim_silence" not in given:
        args.trim = cfg.trim_silence
    if "use_vad" not in given:
        args.vad = cfg.use_vad
    if "--no-popup" not in given and "--popup" not in given:
        args.no_popup = not cfg.show_popup
    if "model_correct" not in given:
        args.correct = cfg.model_correct
    if "hotkey" not in given:
        args.key = cfg.hotkey
    if args.model_dir is None and cfg.model_dir:
        args.model_dir = cfg.model_dir
    if "--threads" not in given and cfg.threads:
        args.threads = cfg.threads
    if args.download_source is None and cfg.download_source:
        args.download_source = cfg.download_source
    return cfg


def ipc_mode(args: argparse.Namespace, argv: list[str] | None = None) -> int:
    """IPC 模式：stdin/stdout 走 NDJSON，不开 HTTP、不启托盘、不装钩子。

    协议硬红线：stdout 只准出现协议 JSON。这里在打开数据库**之前**就把
    `sys.stdout` 换到 stderr，这样 `Store` 的 WAL 警告、任何遗留 `print`
    都不会污染协议；原始 stdout 只交给 `ipc.serve` 写响应。
    """
    from xifeng import ipc

    proto_out = sys.stdout
    # 协议要求 UTF-8：stdin 由 ipc.serve 改；stderr 是 Tauri 侧的日志管道，
    # Windows 默认按 gbk 写，中文日志会变成非法字节——统一成 UTF-8。
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 不是 TextIOWrapper 就保持原样
        pass
    sys.stdout = sys.stderr
    try:
        store = open_store(args)
        if store is None:
            print("[ipc] ⚠ 无数据库（--no-db），使用内存库，数据不落盘", file=sys.stderr, flush=True)
            store = Store(":memory:")
        cfg = apply_user_settings(args, store, argv)
        # D16 运行时：Rust 壳经 asr_begin/feed/end/cancel 驱动推理；
        # 裁剪语义与 ptt_loop 一致（--trim/--vad，命令行与用户设置已在此合流），
        # 后处理/注入不在这里做（Rust 壳负责）。ready 后后台线程 warmup。
        from xifeng.asr_runtime import AsrRuntime

        runtime = AsrRuntime(
            backend=args.backend,
            model_dir=args.model_dir,
            threads=args.threads,
            use_vad=args.vad,
            trim_silence=args.trim,
            model_correct=cfg.model_correct,
            store=store,
        )
        try:
            return ipc.serve(store, cfg, stdout=proto_out, runtime=runtime)
        finally:
            store.close()
    finally:
        sys.stdout = proto_out


def cmd_history(store: Store | None, limit: int) -> int:
    if store is None:
        return 2
    rows = store.recent(limit)
    if not rows:
        print("[db] 暂无历史")
        return 0
    print(f"{'id':>4}  {'时间':19}  {'结果':9}  {'e2e':>5}  {'asr':>5}  {'字':>3}  文本")
    for r in rows:
        text = (r["text"] or "")[:40]
        print(f"{r['id']:>4}  {r['ts'][:19]:19}  {r['outcome']:9}  "
              f"{r['e2e_ms']:>5}  {r['ms_asr']:>5}  {r['chars']:>3}  {text}")
    return 0


def cmd_stats(store: Store | None) -> int:
    if store is None:
        return 2
    s = store.stats()
    print(f"[db] {store.path}")
    print(f"总计 {s['total']} 条，注入成功 {s['injected']}（成功率 {s['success_rate']:.1%}），"
          f"累计 {s['chars']} 字（平均 {s['avg_chars']:.1f}）")
    print(f"e2e  P50={s['e2e']['p50']}ms  P95={s['e2e']['p95']}ms  P99={s['e2e']['p99']}ms")
    print(f"ASR  P50={s['asr']['p50']}ms  P95={s['asr']['p95']}ms  P99={s['asr']['p99']}ms")
    if s["by_outcome"]:
        print("结果分布：" + "  ".join(f"{k}={v}" for k, v in sorted(s["by_outcome"].items())))
    return 0


def cmd_autostart(action: str) -> int:
    """`--autostart on|off|status`：写当前用户的 Run 键（不需要管理员）。"""
    if action == "on":
        try:
            cmd = autostart.enable()
        except Exception as exc:  # noqa: BLE001
            print(f"[autostart] 开启失败：{exc}")
            return 1
        print(f"[autostart] 已开启 → {cmd}")
        return 0
    if action == "off":
        try:
            removed = autostart.disable()
        except Exception as exc:  # noqa: BLE001
            print(f"[autostart] 关闭失败：{exc}")
            return 1
        print("[autostart] 已关闭" if removed else "[autostart] 本来就是关闭的")
        return 0
    print(f"[autostart] {autostart.status_text()}")
    return 0


# ---------------------------------------------------------------- 托盘常驻


def tray_mode(
    pipe: Pipeline,
    args: argparse.Namespace,
    store: Store | None = None,
    sample_rate: int = 16000,
) -> int:
    """常驻托盘：托盘菜单 → 无限 PTT，直到菜单「退出」。

    控制台在 `main()` 里就已经藏好、日志也已重定向，这里只管托盘与循环。
    """
    from xifeng import tray as tray_mod

    stop = threading.Event()
    tray = tray_mod.Tray(on_quit=stop.set)
    try:
        tray.start()
        print("[tray] 已启动：托盘图标右键 → 显示主界面 / 退出", flush=True)
        tray.notify("Xifeng", "已在后台运行：按住 Ctrl 说话，松开输入")
        args.times = 0
        return ptt_loop(pipe, args, store, sample_rate, should_stop=stop.is_set)
    finally:
        tray.stop()
        print("[tray] 已退出", flush=True)


def load_pcm(args: argparse.Namespace) -> np.ndarray:
    if args.wav:
        pcm, rate = audio.read_wav(args.wav)
        if rate != 16000:
            raise SystemExit(f"wav 采样率必须是 16000，实际 {rate}（spike 不做重采样）")
        return pcm
    if args.interactive:
        return record_interactive()
    return record_seconds(args.seconds)


def save_history(store: Store | None, result, mode: str) -> None:  # noqa: ANN001
    """写历史。数据库问题只提示一次，绝不影响本次注入结果。

    无效输入（无文本/丢弃）不写历史：历史表只留有效输入。
    """
    if store is None:
        return
    if not (result.text or "").strip():
        return
    try:
        store.add_history(
            result.trace,
            result.text,
            mode=mode,
            raw_text=result.raw,
            clean_text=result.cleaned,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[db] ⚠ 写历史失败：{exc}", flush=True)


def report(
    pipe: Pipeline,
    pcm: np.ndarray,
    args: argparse.Namespace,
    store: Store | None = None,
    sample_rate: int = 16000,
    mode: str = "wav",
) -> bool:
    """跑一次完整管线并打印结果，返回是否注入成功。"""
    if pcm.size == 0:
        print("[mic] 没采到音频")
        return False
    print(f"[mic] {len(pcm) / sample_rate:.2f}s")

    result = pipe.process_pcm(pcm)
    t = result.trace
    save_history(store, result, mode)

    if args.json:
        print(json.dumps(t.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"[trace] outcome={t.outcome.value} chars={t.chars} fixes={t.fixes} "
            f"trim={t.trimmed_ms}ms asr={t.ms_asr}ms post={t.ms_post}ms "
            f"correct={t.ms_correct}ms "
            f"inject={t.ms_inject}ms e2e={t.e2e_ms}ms strategy={t.injection_strategy}"
            + (f" error={t.error}" if t.error else "")
        )
    if args.print_raw:
        print(f"[asr] raw={result.raw!r}")
    if args.speak_result and result.text:
        print(result.text)

    return t.outcome.value == "Injected"


def ptt_loop(pipe: Pipeline, args: argparse.Namespace, store: Store | None = None,
             sample_rate: int = 16000,
             should_stop: Callable[[], bool] | None = None) -> int:
    """按住热键说话 → 松开 → 识别 → 注入。

    `args.times = 0` 表示一直等（托盘模式）；`should_stop` 让托盘「退出」能中断等待。
    """
    if not hotkey.IS_WINDOWS:
        print("[ptt] 非 Windows 平台不可用（按键采集依赖 Win32）")
        return 2
    infinite = args.times <= 0
    _trig = "按住" if getattr(args, "single_press", False) else "双击后保持长按"
    print(f"[ptt] {hotkey.describe_key(args.key)}：{_trig}说话，松开结束；Esc 取消本次")
    hotkey.ensure_hook()
    backend = hotkey.input_backend()
    print(f"[ptt] 按键采集方式={backend}  {winenv.describe()}")
    if winenv.is_low_integrity():
        print("[ptt] ⚠ 当前进程是 Low 完整性：全局钩子收不到硬件按键（G21）。")
        print("[ptt]   本工作区目录带 Low 标签，目录内的 exe 都受影响。新建目录外的 venv 绕开：")
        print("[ptt]     uv venv %TEMP%\\vi-venv --python 3.11")
        print("[ptt]     set PYTHONPATH=.venv\\Lib\\site-packages")
        print("[ptt]     %TEMP%\\vi-venv\\Scripts\\python.exe run_spike.py --ptt --backend mock --no-inject")
    print(f"[ptt] 误触保护：按住 <{args.min_hold_ms}ms 直接丢弃；单次上限 {args.max_seconds:.0f}s")
    if infinite:
        print("[ptt] 次数=0 → 一直等待（托盘菜单「退出」结束）")
    if args.resident_mic:
        if hotkey.start_mic(sample_rate):
            print("[mic] 常驻流已就绪（开流静默期已在启动时付掉，句首不会丢）")
        else:
            print("[mic] ⚠ 常驻流启动失败，退回「按下才开麦」（首次按键前 0.1–0.4s 可能丢音，G23）")
    else:
        print("[mic] 按需开麦：按下热键才打开麦克风，松开后关闭（不录音时不占用设备）")
    if not args.no_inject:
        print("[inject] " + inject_mod.describe())
    def _wait_hint() -> None:
        print("[ptt] 已等待 5 秒仍未检测到按键 —— 如果你确实按了，说明采集没生效：", flush=True)
        print(f"[ptt]   采集方式={backend}，事件数={hotkey.event_count()}"
              f"（其中注入 {hotkey.injected_count()}）", flush=True)
        print("[ptt]   逐项排查：python key_diag.py（会打印完整性级别、自注入探针与钩子事件数）", flush=True)

    ok_count = 0
    done = 0
    stopped = False
    # 浮窗建一次就复用（Tk 初始化 ~130ms）：内置主题随主界面，每次按键前刷新
    popup = popup_mod.RecordingPopup(
        enabled=not args.no_popup, color=getattr(args, "popup_color", "red")
    )
    trigger = "press" if getattr(args, "single_press", False) else "double"
    try:
        while infinite or done < args.times:
            if should_stop is not None and should_stop():
                stopped = True
                break
            done += 1
            label = f"{done}/∞" if infinite else f"{done}/{args.times}"
            print(f"\n[{label}] 等待按下 {args.key} …", flush=True)
            if store is not None:
                popup.theme = user_settings.load(store).theme

            def _on_start() -> None:
                # 麦克风是按下后才创建的：浮窗要在这时再绑定，否则读不到音量
                popup.bind(hotkey.live_mic())
                popup.show()         # 双击触发已过滤组合键，直接显示
                print("● 录音中… 松开结束", flush=True)

            try:
                res = hotkey.record_ptt(
                    args.key,
                    sample_rate=sample_rate,
                    min_hold_ms=args.min_hold_ms,
                    max_seconds=args.max_seconds,
                    on_start=_on_start,
                    trigger=trigger,
                    on_poll=popup.tick,
                    on_wait=_wait_hint,
                    should_stop=should_stop,
                )
            except KeyboardInterrupt:
                print("\n[ptt] 已退出（Ctrl+C）")
                return 130
            finally:
                popup.close()
            if res.stopped:
                stopped = True
                break
            if res.error:
                print(f"[ptt] ⚠ {res.error}")
                continue
            if res.combo:
                print("[ptt] 检测到组合键（Ctrl+其它键），已忽略本次")
                continue
            if res.cancelled:
                print("[ptt] 已取消（Esc），本次不注入")
                continue
            if res.too_short:
                print(f"[ptt] 按住 {res.seconds * 1000:.0f}ms < {args.min_hold_ms}ms，判定误触，丢弃")
                continue
            if res.truncated:
                print(f"[ptt] 超过上限，已截断到 {args.max_seconds:.0f}s")
            if report(pipe, res.pcm, args, store, sample_rate, mode="ptt"):
                ok_count += 1
    finally:
        hotkey.stop_mic()
        popup.dispose()

    if infinite:
        reason = "托盘退出" if stopped else "结束"
        print(f"\n[ptt] {reason}；本次共注入成功 {ok_count} 次")
        return 0
    print(f"\n[ptt] {ok_count}/{args.times} 次注入成功")
    return 0 if ok_count == args.times else 1


def mode_of(args: argparse.Namespace) -> str:
    if args.ptt:
        return "ptt"
    if args.wav:
        return "wav"
    if args.interactive:
        return "interactive"
    return "seconds"


def main(argv: list[str] | None = None) -> int:
    # 控制台可能是 GBK：先保证任何字符都不会把进程打死（G33）
    applog.tame_console()
    args = parse_args(argv)

    if args.ipc:
        # sidecar 的 stderr 是壳的日志管道，壳按 UTF-8 逐行读（非 UTF-8 直接断流）。
        # Windows 默认 gbk，而迁移日志发生在 ipc_mode() 之前，所以这里提前钉死编码。
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 不是 TextIOWrapper 就保持原样
            pass

    # 改名迁移（D21）：必须在任何 Store/GUI 读路径之前跑，幂等且失败不拦启动。
    # - `IME_DATA_DIR` 显式指定时函数内部直接跳过（用户已接管数据位置）
    # - `--db`/`--no-db` 是显式指定库：不去动默认目录（测试也靠这条隔离真实数据）
    if args.db is None and not args.no_db:
        migrated = paths.migrate_legacy_data()
        if migrated:
            print(
                f"[paths] 已迁移旧版数据 → {paths.data_dir()}（{', '.join(migrated)}）",
                file=sys.stderr,
                flush=True,
            )
    # 自启值迁移只在 Python 托盘模式需要；Tauri 壳自己迁（autostart.rs），
    # IPC/sidecar 进程不该碰注册表。
    if args.tray:
        legacy_autostart = autostart.migrate_legacy()
        if legacy_autostart:
            print(
                f"[autostart] 已迁移旧版自启项（旧值：{legacy_autostart}）",
                file=sys.stderr,
                flush=True,
            )

    # 诊断模式不需要模型，也不录音
    if args.diagnose_keys is not None:
        hotkey.watch_keys(float(args.diagnose_keys))
        return 0

    if args.autostart is not None:
        return cmd_autostart(args.autostart)

    # GUI 是独立进程：不加载模型、不装钩子、不开麦克风
    if args.gui:
        from xifeng import gui
        return gui.run(args.db, args.tab)

    # IPC：Tauri 壳拉起的纯协议进程，不开 HTTP、不启托盘（契约见 ipc-contract.md）
    if args.ipc:
        return ipc_mode(args, argv)

    if args.web:
        from xifeng import webgui
        return webgui.run(args.db, args.tab)

    tray_log: tuple[object, object, object | None] | None = None
    if args.tray:
        args.ptt = True
        if args.times is None:      # 没显式指定时托盘应一直等
            args.times = 0
        from xifeng import tray as tray_mod
        tray_log = (sys.stdout, sys.stderr, applog.setup(force_file=True))
        tray_mod.hide_console()     # 先隐藏再加载模型，开机自启不会闪一下黑窗
        if not tray_mod.acquire_single_instance():
            print("[tray] 已有实例在运行，本次退出", flush=True)
            applog.restore(tray_log[2], tray_log[0], tray_log[1])
            return 0
    elif args.times is None:
        args.times = 1

    store = open_store(args)
    user_cfg = apply_user_settings(args, store, argv)
    try:
        # --text：直接喂文本，不加载 ASR 模型；--correct 时才加载纠错模型
        if args.text is not None:
            post_cfg = user_settings.post_config(user_cfg)
            cleaned = postprocess.process(args.text, post_cfg)
            if cleaned is not None and args.correct:
                if correction.load(threads=args.threads):
                    correction.warmup()      # 冷启动 ~4s，一次性的
                    fix = correction.correct(cleaned)
                    if fix.applied:
                        cleaned = postprocess.final_check(
                            postprocess.normalize(fix.text, post_cfg)
                        )
                        print(f"[correct] applied fixes={fix.fixes} {fix.ms}ms")
                    else:
                        print(f"[correct] 保留原文（{fix.reason}）{fix.ms}ms")
                else:
                    print(f"[correct] 模型不可用：{correction.last_error()}")
            print(f"[post] raw={args.text!r}")
            print(f"[post] cleaned={cleaned!r}")
            if cleaned is None:
                print("[post] 判定：不注入")
                return 0
            if args.no_inject:
                return 0
            snap = inject_mod.snapshot_focus()
            print("[inject] " + inject_mod.describe())
            res = inject_mod.inject_ex(
                cleaned,
                snap,
                inject_mod.InjectConfig(
                    strategy=args.inject,
                    restore_focus=args.restore_focus,
                    clipboard_restore_ms=args.clipboard_restore_ms,
                ),
            )
            print(f"[inject] {res.outcome.value}（策略={res.strategy}）")
            return 0 if res.outcome is inject_mod.InjectOutcome.INJECTED else 1

        # --- 数据库只读子命令
        if args.history is not None:
            return cmd_history(store, args.history)
        if args.stats:
            return cmd_stats(store)

        if store is not None:
            print(f"[db] {describe_db(store)}")

        sink = TraceSink(Path(args.trace)) if args.trace else None
        cfg = PipelineConfig(
            use_vad=args.vad,
            do_inject=not args.no_inject,
            restore_focus=args.restore_focus,
            model_correct=bool(args.correct),
            trim_silence=args.trim,
            inject_strategy=args.inject,
            clipboard_restore_ms=args.clipboard_restore_ms,
            post=user_settings.post_config(user_cfg),
            asr_threads=args.threads,
            download_source=args.download_source,
        )
        pipe = Pipeline(cfg, sink)

        print(f"[asr] backend={args.backend} model_dir={args.model_dir or os.environ.get('IME_MODELS_DIR') or '(默认)'}")
        try:
            pipe.warmup(
                backend=args.backend,
                model_dir=args.model_dir,
                threads=args.threads,
                download_source=args.download_source,
            )
        except asr.AsrError as exc:
            print(f"[asr] 加载失败：{exc}", file=sys.stderr)
            return 2
        print(f"[asr] 实际后端={asr.backend()}")

        if args.ptt:
            if args.tray:
                return tray_mode(pipe, args, store, cfg.sample_rate)
            return ptt_loop(pipe, args, store, cfg.sample_rate)

        pcm = load_pcm(args)
        if not args.no_inject:
            print("[inject] " + inject_mod.describe())
        return 0 if report(pipe, pcm, args, store, cfg.sample_rate, mode_of(args)) else 1
    finally:
        if store is not None:
            store.close()
        if tray_log is not None:
            applog.restore(tray_log[2], tray_log[0], tray_log[1])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断（Ctrl+C）")
        raise SystemExit(130) from None
