"""用户配置：外置 JSON 文件（`%LOCALAPPDATA%\\Xifeng\\config.json`）。

为什么从 SQLite 搬出来：用户要能直接编辑配置（改热键、改识别语言），
JSON 可读、可手改、可被脚本读写；SQLite 里的 `settings` 表做不到"看得懂"。
历史/统计仍留在 SQLite（那是数据，不是配置）。

迁移（一次性、幂等）：
- 首次启动时若 JSON 不存在而旧 SQLite `settings` 表里有值 → 把旧值搬进 JSON，
  写一个 `migrated_from` 标记，之后不再重复搬。
- JSON 存在 → **JSON 优先**（用户手改过就不该被旧库覆盖）。
- JSON 损坏（非法 JSON / 不是对象）→ 改名备份 `config.json.bak`，用默认值重建，
  绝不因为一个坏文件让程序起不来。

写入策略：**先写临时文件再原子替换**，避免写到一半断电留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import paths

CONFIG_NAME = "config.json"
#: 坏文件备份后缀（同名覆盖会丢现场，故用带序号的）
BAD_SUFFIX = ".bad"

#: 配置文件的 schema 版本（未来加字段时用于迁移）
SCHEMA_VERSION = 1


def config_path() -> Path:
    """配置文件路径：`%LOCALAPPDATA%\\Xifeng\\config.json`。

    与 `xifeng.db` / `xifeng.log` / `models\\` 同目录——统一根（D15/D21）。
    `IME_DATA_DIR` 显式指定时跟随它（便携/沙箱调试用）。
    """
    return paths.data_dir() / CONFIG_NAME


def _read_json(path: Path) -> dict[str, Any] | None:
    """读 JSON 对象；读不了或不是对象返回 None。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _backup_bad(path: Path) -> str:
    """把坏文件改名备份，返回新文件名（失败则原地留一份 .bad）。"""
    for i in range(1, 100):
        cand = path.with_name(f"{path.name}.{i}{BAD_SUFFIX}")
        if not cand.exists():
            try:
                path.rename(cand)
                return cand.name
            except OSError:
                break
    try:
        cand = path.with_name(f"{path.name}{BAD_SUFFIX}")
        path.rename(cand)
        return cand.name
    except OSError:
        return ""


def _write_json_atomic(path: Path, data: dict[str, Any]) -> bool:
    """原子写：先写同目录临时文件，再 `os.replace` 覆盖。

    临时文件必须在**同目录**（跨卷 `os.replace` 会失败）；同目录也保证
    替换是同一文件系统内的原子操作。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None  # 已交给 fdopen 管理
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- 读写入口


def load_raw() -> tuple[dict[str, Any], str]:
    """读原始配置字典。

    返回 `(data, note)`：`note` 是给日志看的一句话（"新建" / "损坏已备份" /
    "读取失败" / ""），**不含配置内容**（日志禁止记录敏感值，这里也保持一致）。
    """
    path = config_path()
    if not path.exists():
        return {}, "新建"
    data = _read_json(path)
    if data is not None:
        return data, ""
    name = _backup_bad(path)
    return {}, f"损坏已备份为 {name}" if name else "损坏且备份失败，已用默认值"


def save_raw(data: dict[str, Any]) -> bool:
    """写配置文件（原子）。失败返回 False，调用方负责降级。"""
    payload = dict(data)
    payload["schema_version"] = SCHEMA_VERSION
    return _write_json_atomic(config_path(), payload)


def migrate_from_store(store: Any) -> bool:
    """把旧 SQLite `settings` 表里的值搬进 JSON（一次性、幂等）。

    - JSON 已存在 → 不动（用户手改过的配置优先，旧库不再打扰）。
    - 旧库没有值 → 不动（等用户第一次保存时自然生成）。
    - 搬完在 JSON 里写 `migrated_from: "sqlite"`，重复调用直接返回 False。

    `store` 是 `Store` 实例（鸭子类型：只需 `get_setting`）。
    """
    path = config_path()
    if path.exists():
        return False
    try:
        values = {k: v for k, v in _all_store_settings(store).items() if v is not None}
    except Exception:  # noqa: BLE001 - 旧库坏了不影响启动
        return False
    if not values:
        return False
    payload: dict[str, Any] = dict(values)
    payload["migrated_from"] = "sqlite"
    payload["schema_version"] = SCHEMA_VERSION
    return _write_json_atomic(path, payload)


def _all_store_settings(store: Any) -> dict[str, str]:
    """读旧库的全部设置键值（`settings` 表）。"""
    out: dict[str, str] = {}
    try:
        conn = store._conn  # noqa: SLF001 - 迁移期一次性读取内部句柄
    except AttributeError:
        return out
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    except Exception:  # noqa: BLE001 - 表不存在（旧库）时静默跳过
        return out
    for r in rows:
        try:
            out[str(r["key"])] = str(r["value"])
        except (KeyError, IndexError, TypeError):
            continue
    return out
