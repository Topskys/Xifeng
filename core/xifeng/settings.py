"""用户设置：配置文件读写 + 校验。

为什么单独一层：设置项要同时被三个入口消费（GUI 保存、命令行默认值、托盘启动），
散落在 `run_spike` 里会变成"改一处漏两处"。这里只做**读写 + 校验**，不碰 tkinter。

优先级（从高到低）：
1. 命令行显式参数（`--inject clipboard` 覆盖设置）
2. 配置文件 `%LOCALAPPDATA%\\Xifeng\\config.json`（可手改，见 `config.py`）
3. 代码默认值

存储：**JSON 文件**（用户要能直接看懂并编辑）。旧版的 SQLite `settings` 表
在首次启动时一次性迁移进 JSON，之后只读 JSON（见 `config.migrate_from_store`）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from . import asr, config, hotkey, postprocess

INJECT_STRATEGIES = ("auto", "unicode", "clipboard")
FILLER_LEVELS = ("off", "standard", "deep")
FINAL_PUNCT = ("keep", "strip")
DOWNLOAD_SOURCES = ("modelscope", "hf-mirror", "github", "local")
THEMES = ("dark", "light")

#: ASR 识别语言（SenseVoice 支持的语种）。G22：auto 会把普通话判成粤语，
#: 所以必须显式指定；这里给的是用户可选清单。
LANGUAGES = ("zh", "en", "ja", "ko", "yue")

#: 语言的用户可见名（配置页下拉用）
LANGUAGE_LABEL: dict[str, str] = {
    "zh": "中文（普通话）",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "yue": "粤语",
}


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
    language: str = asr.DEFAULT_LANGUAGE  # ASR 识别语言：zh | en | ja | ko | yue

    def as_dict(self) -> dict[str, str]:
        return {k: _encode(v) for k, v in asdict(self).items()}


DEFAULTS = Settings()

# 旧热键值 → 现在的具体一侧（左右互斥后可选项只有 lctrl/rctrl/lshift/rshift）
_LEGACY_HOTKEYS = {"ctrl": "rctrl", "shift": "lshift"}

#: 不是用户设置的元数据键（迁移标记等），读写时忽略
_META_KEYS = frozenset({"migrated_from", "schema_version"})


def _encode(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _as_bool(raw: object, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: object, default: int, lo: int, hi: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _as_choice(raw: object, choices: tuple[str, ...], default: str) -> str:
    # 配置是用户手改的 JSON，类型可能是数字/布尔/列表——一律当字符串处理，
    # 不认识的取值退回默认，**绝不抛异常**（一个坏值不该让程序起不来）。
    value = "" if raw is None else str(raw).strip()
    return value if value in choices else default


def load(store: object | None = None) -> Settings:
    """读设置；文件坏了/值非法时退回默认，绝不让 GUI 打不开。

    `store` 只用于**一次性迁移**（旧 SQLite → JSON）：JSON 已存在时完全不看它。
    """
    try:
        config.migrate_from_store(store) if store is not None else None
    except Exception:  # noqa: BLE001 - 迁移失败也要能启动
        pass
    raw, _note = config.load_raw()
    return from_raw(raw)


def from_raw(raw: dict) -> Settings:
    """从原始字典构造设置（非法值逐项兜底，缺项用默认）。"""
    if not isinstance(raw, dict):
        return DEFAULTS
    get = raw.get
    # 旧热键兼容：`ctrl`/`shift` 曾表示"左右均可"，现在左右互斥，
    # 读到旧值时归一到具体一侧（ctrl→右 Ctrl、shift→左 Shift），
    # 这样老配置不用重写、UI 也能显示真实生效的那一侧。
    hot_raw = str(get("hotkey") or "").strip()
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
        model_dir=str(get("model_dir") or "").strip(),        threads=_as_int(get("threads"), DEFAULTS.threads, 1, 32),
        download_source=_as_choice(
            get("download_source"), DOWNLOAD_SOURCES, DEFAULTS.download_source
        ),
        theme=_as_choice(get("theme"), THEMES, DEFAULTS.theme),
        show_popup=_as_bool(get("show_popup"), DEFAULTS.show_popup),
        model_correct=_as_bool(get("model_correct"), DEFAULTS.model_correct),
        popup_color=_as_choice(get("popup_color"), ("red", "cyan", "green", "white"), DEFAULTS.popup_color),
        language=_as_choice(get("language"), LANGUAGES, DEFAULTS.language),
    )


def to_raw(settings: Settings) -> dict:
    """设置 → 原始字典（写文件用）。元数据键不带。"""
    return {k: v for k, v in settings.as_dict().items() if k not in _META_KEYS}


def save(store: object | None, settings: Settings) -> bool:
    """写配置文件。返回是否成功。

    `store` 参数保留只为**调用点兼容**（旧签名是 `save(store, settings)`），
    新实现不再写 SQLite。
    """
    del store  # 新实现只用 JSON 文件
    return config.save_raw(to_raw(settings))


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
        language=_as_choice(s.language, LANGUAGES, DEFAULTS.language),
    )
