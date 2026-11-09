"""SQLite 持久化：输入历史与设置。

为什么不用 JSONL：历史要按时间/结果/耗时查询，设置要能被 CLI 与 GUI 共用 ——
这两类都是"有结构的数据"，SQLite 是标准库自带、零依赖、单文件。

隐私边界（AGENTS.md）：
- 数据库只在本机（`%LOCALAPPDATA%\\VoiceIME\\voiceime.db` 或 `IME_DB`）
- **日志**仍然禁止记录识别文本全文；历史表是用户可见数据，可通过 `--no-store-text` 关闭
- 音频从不落库

并发：单进程写入 + WAL；`busy_timeout=3000` 防 GUI/CLI 同时打开时直接报错。
迁移用 `PRAGMA user_version`，只增不改（旧库必须能直接打开）。

v3 的 `terms` 表是旧版规则纠错遗留；v0.23 起纠错改为本地模型（correction.py），
程序不再读写它，旧库里的表原样保留（不删数据）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import paths
from .trace import Outcome, Trace, percentiles

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    mode        TEXT    NOT NULL DEFAULT 'ptt',
    outcome     TEXT    NOT NULL,
    text        TEXT,
    -- v2：三级对照（原文 / 清理 / 纠错后）。同样受 store_text 开关约束
    raw_text    TEXT,
    clean_text  TEXT,
    chars       INTEGER NOT NULL DEFAULT 0,
    speech_ms   INTEGER NOT NULL DEFAULT 0,
    ms_asr      INTEGER NOT NULL DEFAULT 0,
    ms_post     INTEGER NOT NULL DEFAULT 0,
    ms_inject   INTEGER NOT NULL DEFAULT 0,
    e2e_ms      INTEGER NOT NULL DEFAULT 0,
    fixes       INTEGER NOT NULL DEFAULT 0,
    strategy    TEXT    NOT NULL DEFAULT '',
    error       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """一个进程一个实例；线程内使用（sqlite3 默认 check_same_thread）。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        store_text: bool = True,
        timeout: float = 3.0,
    ) -> None:
        self.path = Path(path) if path is not None else paths.db_path()
        self.store_text = store_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：--web 模式 Store 在主线程建、HTTP 线程用
        # （HTTPServer 单线程串行处理，无并发写）；其它调用方维持单线程使用
        self._conn = sqlite3.connect(str(self.path), timeout=timeout, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL 要在库旁边建 -wal/-shm；只读介质上会失败，退回默认日志模式即可（G25）
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError as exc:
            print(f"[db] ⚠ WAL 不可用（{exc}），改用默认日志模式", flush=True)
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        self._migrate()

    # ------------------------------------------------------------ 生命周期

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}，请升级程序"
            )
        if version < 1:
            self._conn.executescript(_SCHEMA)
            version = 1
        else:
            # 表已存在也要保证脚本幂等（新增列时在这里补 ALTER）
            self._conn.executescript(_SCHEMA)

        if version < 2:
            # v2：历史卡片要展示"原文 → 清理 → 纠错"三级对照。
            # 旧库保住 text（最终注入文本），前两级留空，不伪造数据。
            existing = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(history)").fetchall()
            }
            for column in ("raw_text", "clean_text"):
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE history ADD COLUMN {column} TEXT")

        if version < 3:
            # v3：历史页要显示"纠正 N 处"。Trace.fixes 一直是 int，此前没落库。
            existing = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(history)").fetchall()
            }
            if "fixes" not in existing:
                self._conn.execute(
                    "ALTER TABLE history ADD COLUMN fixes INTEGER NOT NULL DEFAULT 0"
                )

        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    # ------------------------------------------------------------ 历史

    def add_history(
        self,
        trace: Trace,
        text: str | None = None,
        mode: str = "ptt",
        raw_text: str | None = None,
        clean_text: str | None = None,
    ) -> int:
        """写一条历史。`text=None` 或关闭 store_text 时不落文本（只留长度）。"""
        keep = text if (self.store_text and text) else None
        raw = raw_text if (self.store_text and raw_text) else None
        clean = clean_text if (self.store_text and clean_text) else None
        cur = self._conn.execute(
            "INSERT INTO history(ts, mode, outcome, text, raw_text, clean_text, chars,"
            " speech_ms, ms_asr, ms_post, ms_inject, e2e_ms, fixes, strategy, error)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trace.ts or _now(),
                mode,
                trace.outcome.value,
                keep,
                raw,
                clean,
                trace.chars,
                trace.speech_ms,
                trace.ms_asr,
                trace.ms_post,
                trace.ms_inject,
                trace.e2e_ms,
                int(trace.fixes or 0),
                trace.injection_strategy,
                trace.error[:200],
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def history_page(
        self, days: int | None = None, offset: int = 0, limit: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        """分页历史（新→旧），带周期过滤；返回 (rows, 周期内总条数)。"""
        since = None
        if days is not None:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where = " WHERE ts >= ?" if since else ""
        params: list = []
        if since:
            params.append(since)
        total = int(
            self._conn.execute(f"SELECT COUNT(*) FROM history{where}", params).fetchone()[0]
        )
        rows = self._conn.execute(
            f"SELECT * FROM history{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, max(0, limit), max(0, offset)],
        ).fetchall()
        return [dict(r) for r in rows], total

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])

    def delete_history(self, row_id: int) -> bool:
        """删单条历史（GUI 卡片菜单用）。返回是否真的删掉了。"""
        cur = self._conn.execute("DELETE FROM history WHERE id=?", (int(row_id),))
        self._conn.commit()
        return (cur.rowcount or 0) > 0

    def stats(self, days: int | None = None) -> dict[str, Any]:
        """聚合统计：次数、成功率、P50/P95（e2e 与 ASR）、平均字数。"""
        where, params = "", ()
        if days is not None:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            where, params = " WHERE ts >= ?", (since,)
        rows = self._conn.execute(
            "SELECT outcome, e2e_ms, ms_asr, chars FROM history" + where, params
        ).fetchall()
        total = len(rows)
        injected = sum(1 for r in rows if r["outcome"] == Outcome.INJECTED.value)
        e2e = [int(r["e2e_ms"]) for r in rows if r["e2e_ms"] > 0]
        asr = [int(r["ms_asr"]) for r in rows if r["ms_asr"] > 0]
        chars = sum(int(r["chars"]) for r in rows)
        timeouts = sum(1 for r in rows if int(r["e2e_ms"]) > 5000)
        return {
            "total": total,
            "injected": injected,
            "success_rate": (injected / total) if total else 0.0,
            "chars": chars,
            "avg_chars": (chars / total) if total else 0.0,
            "e2e": percentiles(e2e),
            "asr": percentiles(asr),
            "timeouts": timeouts,
            "by_outcome": _count_by(rows, "outcome"),
        }

    def prune(self, keep_days: int) -> int:
        """删除早于 keep_days 的历史，返回删除条数（0 表示不清理）。"""
        if keep_days <= 0:
            return 0
        since = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = self._conn.execute("DELETE FROM history WHERE ts < ?", (since,))
        self._conn.commit()
        return int(cur.rowcount or 0)

    def daily(self, days: int = 14) -> list[dict[str, Any]]:
        """按日聚合（最近 N 天，旧→新）：次数、字数、平均端到端延迟，给统计页趋势图。"""
        rows = self._conn.execute(
            "SELECT substr(ts, 1, 10) AS d, COUNT(*) AS n, COALESCE(SUM(chars), 0) AS c,"
            " COALESCE(AVG(CASE WHEN e2e_ms > 0 THEN e2e_ms END), 0) AS e"
            " FROM history GROUP BY d ORDER BY d DESC LIMIT ?",
            (max(1, days),),
        ).fetchall()
        return [
            {"date": r["d"], "count": int(r["n"]), "chars": int(r["c"]), "avg_e2e": int(r["e"])}
            for r in reversed(rows)
        ]

    # ------------------------------------------------------------ 设置

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _count_by(rows: list[sqlite3.Row], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r[field])
        out[k] = out.get(k, 0) + 1
    return out


def describe(store: Store) -> str:
    """给 CLI 打印用的一行摘要（不含文本）。"""
    s = store.stats()
    return (
        f"db={store.path} total={s['total']} injected={s['injected']} "
        f"e2e_p50={s['e2e']['p50']}ms e2e_p95={s['e2e']['p95']}ms"
    )
