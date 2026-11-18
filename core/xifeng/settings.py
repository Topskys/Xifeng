"""用户设置：GUI 写、托盘/CLI 读。

为什么单独一层：设置项要同时被三个入口消费（GUI 保存、命令行默认值、托盘启动），
散落在 `run_spike` 里会变成"改一处漏两处"。这里只做**读写 + 校验**，不碰 tkinter。

优先级（从高到低）：
1. 命令行显式参数（`--inject clipboard` 覆盖设置）
2. SQLite `settings` 表
3. 代码默认值

存储用 `settings(key, value)` 表，不做 schema 迁移 —— 新键直接读默认值即可。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from . import asr, hotkey, postprocess
from .store import Store

INJECT_STRATEGIES = ("auto", "unicode", "clipboard")
FILLER_LEVELS = ("off", "standard", "deep")
FINAL_PUNCT = ("keep", "strip")
DOWNLOAD_SOURCES = ("modelscope", "hf-mirror", "github", "local")
THEMES = ("dark", "light")


@dataclass(frozen=True)
class Settings:
    inject_strategy: str = "auto"        # auto | unicode | clipboard
    restore_focus: bool = False          # 焦点被切走时尝试抢回
    clipboard_restore_ms: int = 500      # 剪贴板兜底后恢复原内容的等待
    filler_level: str = "standard"       # off | standard | deep
    final_punct: str = "keep"            # keep | strip
    space_between_cjk_latin: bool = True
    trim_silence: bool = True            # 裁首尾静音
    use_vad: bool = False                # 录音段内 Silero 双阈值切分（非免按键）
    hotkey: str = hotkey.DEFAULT_KEY
    model_dir: str = ""                  # 空 = 用 paths.models_dir()
    threads: int = asr.DEFAULT_THREADS   # ASR 推理线程数
    download_source: str = "modelscope"  # 模型下载源：modelscope | hf-mirror | github | local
    theme: str = "dark"                  # 界面主题：dark | light
    show_popup: bool = True              # PTT 录音指示浮窗
    model_correct: bool = False          # 本地模型纠错（默认关：+0.5–1.5s，G32）
    popup_color: str = "red"             # 录音浮窗音效颜色：red | cyan | green | white

    def as_dict(self) -> dict[str, str]:
        return {k: _encode(v) for k, v in asdict(self).items()}


DEFAULTS = Settings()

# 旧热键值 → 现在的具体一侧（左右互斥后可选项只有 lctrl/rctrl/lshift/rshift）
_LEGACY_HOTKEYS = {"ctrl": "rctrl", "shift": "lshift"}


def _encode(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _as_choice(raw: str | None, choices: tuple[str, ...], default: str) -> str:
    value = (raw or "").strip()
    return value if value in choices else default


def load(store: Store | None) -> Settings:
    """读设置；库不可用/值非法时退回默认，绝不让 GUI 打不开。"""
    if store is None:
        return DEFAULTS
    try:
        get = store.get_setting
        # 旧值兼容：`ctrl`/`shift` 曾经表示"左右均可"，现在左右互斥，
        # 读到旧值时归一到具体一侧（ctrl→右 Ctrl、shift→左 Shift），
        # 这样老库不用重写、UI 也能显示真实生效的那一侧。
        hot_raw = (get("hotkey") or "").strip()
        hot = _as_choice(
            _LEGACY_HOTKEYS.get(hot_raw, hot_raw),
            tuple(sorted(hotkey.KEYS)),
            DEFAULTS.hotkey,
        )
        return Settings(
            inject_strategy=_as_choice(
                get("inject_strategy"), INJECT_STRATEGIES, DEFAULTS.inject_strategy
            ),
            restore_focus=_as_bool(get("restore_focus"), DEFAULTS.restore_focus),
            clipboard_restore_ms=_as_int(
                get("clipboard_restore_ms"), DEFAULTS.clipboard_restore_ms, 0, 5000
            ),
            filler_level=_as_choice(get("filler_level"), FILLER_LEVELS, DEFAULTS.filler_level),
            final_punct=_as_choice(get("final_punct"), FINAL_PUNCT, DEFAULTS.final_punct),
            space_between_cjk_latin=_as_bool(
                get("space_between_cjk_latin"), DEFAULTS.space_between_cjk_latin
            ),
            trim_silence=_as_bool(get("trim_silence"), DEFAULTS.trim_silence),
            use_vad=_as_bool(get("use_vad"), DEFAULTS.use_vad),
            hotkey=hot,
            model_dir=(get("model_dir") or "").strip(),
            threads=_as_int(get("threads"), DEFAULTS.threads, 1, 32),
            download_source=_as_choice(
                get("download_source"), DOWNLOAD_SOURCES, DEFAULTS.download_source
            ),
            theme=_as_choice(get("theme"), THEMES, DEFAULTS.theme),
            show_popup=_as_bool(get("show_popup"), DEFAULTS.show_popup),
            model_correct=_as_bool(get("model_correct"), DEFAULTS.model_correct),
            popup_color=_as_choice(get("popup_color"), ("red", "cyan", "green", "white"), DEFAULTS.popup_color),
        )
    except Exception:  # noqa: BLE001 - 设置坏了也要能启动
        return DEFAULTS


def save(store: Store, settings: Settings) -> None:
    for key, value in settings.as_dict().items():
        store.set_setting(key, value)


def post_config(settings: Settings) -> postprocess.PostConfig:
    return postprocess.PostConfig(
        filler_level=settings.filler_level,
        final_punct=settings.final_punct,
        space_between_cjk_latin=settings.space_between_cjk_latin,
    )


def update(settings: Settings, **changes: object) -> Settings:
    """带校验的拷贝更新，GUI 保存时用。"""
    s = replace(settings, **changes)
    return Settings(
        inject_strategy=_as_choice(s.inject_strategy, INJECT_STRATEGIES, DEFAULTS.inject_strategy),
        restore_focus=bool(s.restore_focus),
        clipboard_restore_ms=max(0, min(5000, int(s.clipboard_restore_ms))),
        filler_level=_as_choice(s.filler_level, FILLER_LEVELS, DEFAULTS.filler_level),
        final_punct=_as_choice(s.final_punct, FINAL_PUNCT, DEFAULTS.final_punct),
        space_between_cjk_latin=bool(s.space_between_cjk_latin),
        trim_silence=bool(s.trim_silence),
        use_vad=bool(s.use_vad),
        hotkey=_as_choice(s.hotkey, tuple(sorted(hotkey.KEYS)), DEFAULTS.hotkey),
        model_dir=str(s.model_dir or "").strip(),
        threads=max(1, min(32, int(s.threads))),
        download_source=_as_choice(
            s.download_source, DOWNLOAD_SOURCES, DEFAULTS.download_source
        ),
        theme=_as_choice(s.theme, THEMES, DEFAULTS.theme),
        show_popup=bool(s.show_popup),
        model_correct=bool(s.model_correct),
        popup_color=_as_choice(s.popup_color, ("red", "cyan", "green", "white"), DEFAULTS.popup_color),
    )
