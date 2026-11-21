"""外置 JSON 配置（`config.py`）的回归。

关注三件事：
- 读/写/往返：写进去能读回来，类型不丢
- 坏文件不致命：损坏的 JSON 会被备份，程序仍能启动
- 原子写：写到一半断电不留半个文件（临时文件 + replace）
"""

from __future__ import annotations

import json

from xifeng import config, settings as user_settings


def test_config_path_is_json_in_data_dir():
    p = config.config_path()
    assert p.name == "config.json"
    assert p.parent == config.paths.data_dir()


def test_save_and_load_roundtrip():
    cfg = user_settings.DEFAULTS
    cfg = user_settings.update(cfg, language="en", hotkey="lctrl", threads=4)
    assert user_settings.save(None, cfg) is True

    raw, note = config.load_raw()
    assert note == ""
    assert raw["language"] == "en"
    assert raw["hotkey"] == "lctrl"
    assert raw["threads"] == "4"

    back = user_settings.load(None)
    assert back.language == "en"
    assert back.hotkey == "lctrl"
    assert back.threads == 4


def test_file_is_human_readable_json():
    user_settings.save(None, user_settings.update(user_settings.DEFAULTS, language="ja"))
    text = config.config_path().read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert parsed["language"] == "ja"
    # 缩进 + 非 ASCII 不转义：用户要能直接读懂并手改
    assert "\n" in text
    assert "\\u" not in text


def test_missing_file_returns_defaults_not_error():
    raw, note = config.load_raw()
    assert raw == {}
    assert note == "新建"
    assert user_settings.load(None).language == user_settings.DEFAULTS.language


def test_corrupt_file_is_backed_up_and_defaults_used():
    p = config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json at all", encoding="utf-8")

    raw, note = config.load_raw()
    assert raw == {}
    assert "损坏" in note
    # 坏文件被改名备份，原地不再有损坏内容
    assert not p.exists()
    assert any(f.name.startswith("config.json") and f.name.endswith(".bad") for f in p.parent.iterdir())
    # 仍然能用默认值启动
    assert user_settings.load(None).language == user_settings.DEFAULTS.language


def test_non_object_json_is_treated_as_corrupt():
    p = config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1, 2, 3]", encoding="utf-8")
    raw, note = config.load_raw()
    assert raw == {}
    assert "损坏" in note


def test_write_is_atomic_no_temp_left_behind():
    user_settings.save(None, user_settings.DEFAULTS)
    p = config.config_path()
    leftovers = [f.name for f in p.parent.iterdir() if f.name.endswith(".tmp")]
    assert leftovers == []


def test_invalid_values_fall_back_to_defaults():
    config.save_raw(
        {
            "language": "klingon",       # 不支持的语言
            "threads": "not-a-number",   # 不是数字
            "theme": 12345,              # 类型不对
            "hotkey": "bogus",
        }
    )
    cfg = user_settings.load(None)
    assert cfg.language == user_settings.DEFAULTS.language
    assert cfg.threads == user_settings.DEFAULTS.threads
    assert cfg.theme == user_settings.DEFAULTS.theme
    assert cfg.hotkey == user_settings.DEFAULTS.hotkey


def test_legacy_hotkey_still_normalized_from_json():
    config.save_raw({"hotkey": "ctrl"})
    assert user_settings.load(None).hotkey == "rctrl"
    config.save_raw({"hotkey": "shift"})
    assert user_settings.load(None).hotkey == "lshift"


def test_resident_mic_defaults_off_and_roundtrips():
    # 旧配置没有这个字段：必须保持按需开麦，不能因升级变成常驻占麦。
    config.save_raw({"language": "zh"})
    assert user_settings.load(None).resident_mic is False

    enabled = user_settings.update(user_settings.DEFAULTS, resident_mic=True)
    assert user_settings.save(None, enabled) is True
    assert user_settings.load(None).resident_mic is True
    assert config.load_raw()[0]["resident_mic"] == "1"

    disabled = user_settings.update(enabled, resident_mic=False)
    assert user_settings.save(None, disabled) is True
    assert user_settings.load(None).resident_mic is False


def test_use_itn_defaults_on_and_roundtrips():
    # 新配置与旧配置缺字段都默认开；用户显式关闭后必须原样保留。
    config.save_raw({"language": "zh"})
    assert user_settings.load(None).use_itn is True

    disabled = user_settings.update(user_settings.DEFAULTS, use_itn=False)
    assert user_settings.save(None, disabled) is True
    assert user_settings.load(None).use_itn is False
    assert config.load_raw()[0]["use_itn"] == "0"

    enabled = user_settings.update(disabled, use_itn=True)
    assert user_settings.save(None, enabled) is True
    assert user_settings.load(None).use_itn is True
    assert config.load_raw()[0]["use_itn"] == "1"


def test_metadata_keys_are_not_treated_as_settings():
    config.save_raw({"language": "en", "migrated_from": "sqlite", "schema_version": 1})
    cfg = user_settings.load(None)
    assert cfg.language == "en"
    # 元数据不该被当成用户设置项回写
    assert "migrated_from" not in user_settings.to_raw(cfg)
