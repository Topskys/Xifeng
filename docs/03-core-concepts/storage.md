# 存储（SQLite）

VoiceIME 只有两类**有结构的数据**：输入历史、设置。它们都进一个 SQLite 文件，
用 Python 标准库 `sqlite3`（零依赖、单文件、GUI 直接读同一个库）。

**音频从不落库**；文本默认进历史表，可关闭（见「隐私边界」）。

## 表结构

```sql
CREATE TABLE history (          -- 一次输入一条，只记耗时/长度/结果
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,          -- 本地时区 ISO8601（毫秒）
    mode        TEXT    NOT NULL,          -- ptt / wav / seconds / interactive
    outcome     TEXT    NOT NULL,          -- Injected / Cancelled / Dropped / Failed
    text        TEXT,                      -- 注入的最终文本；关闭 store_text 时为 NULL
    chars       INTEGER NOT NULL,
    speech_ms   INTEGER NOT NULL,
    ms_asr      INTEGER NOT NULL,
    ms_post     INTEGER NOT NULL,
    ms_inject   INTEGER NOT NULL,
    e2e_ms      INTEGER NOT NULL,
    strategy    TEXT    NOT NULL,          -- unicode / clipboard / none（dry-run）
    error       TEXT    NOT NULL
);

CREATE TABLE terms (            -- 旧版规则纠错遗留；程序已不再读写，旧库原样保留
    wrong   TEXT PRIMARY KEY,
    right   TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    hits    INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);

CREATE TABLE settings (         -- 键值对，CLI 与 GUI 共用
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

> v0.23 起纠错改由本地模型（`correction.py`，G32）完成，新库不再建 `terms` 表，
> 旧库里的表也不会被删（用户数据不动），只是没有任何代码读写它。

`history` 上只有 `ts` 索引（查询都是"最近 N 条"和"某时间段聚合"）。

## 迁移

用 `PRAGMA user_version`，**只增不改**：

- 打开时 `version > SCHEMA_VERSION` → 直接报错（旧程序不许写新库，避免悄悄丢字段）
- `version < SCHEMA_VERSION` → 执行幂等 DDL（`CREATE TABLE IF NOT EXISTS`），再写版本号
- 新增列时在同一处补 `ALTER TABLE … ADD COLUMN`（SQLite 不支持 `ADD COLUMN IF NOT EXISTS`）

库坏了不会拦住输入：`run_spike.open_store()` 捕获异常后打印一行警告，本次不记历史，
语音输入照常工作。

## 文件位置

按优先级（`voiceime_spike/paths.py`）：

| 顺序 | 条件 | 路径 |
|---|---|---|
| 1 | 环境变量 `IME_DB` | 指定整个文件路径 |
| 2 | 环境变量 `IME_DATA_DIR` | `<该目录>/voiceime.db` |
| 3 | `%LOCALAPPDATA%` 可写（源码与冻结形态都走这条） | `%LOCALAPPDATA%\VoiceIME\voiceime.db` |
| 4 | 冻结形态且 `%LOCALAPPDATA%` 不可写 | `<exe 同级>/data/voiceime.db` |
| 5 | 源码形态且 `%LOCALAPPDATA%` 不可写（Low 进程） | `<spike>/.data/voiceime.db` |

为什么优先 `%LOCALAPPDATA%` 而不是 exe 同级：装到 `Program Files` 时 exe 目录不可写；
而且**升级会整目录替换**，历史会跟着丢。第 4/5 条只是保底（便携版、Low 进程），
第 5 条是本工作区的常态：仓库目录带 Low 标签，写 `%LOCALAPPDATA%` 会被
no-write-up 拒绝（G25），因此先探测可写性再决定。

## 隐私边界

| 数据 | 是否落盘 | 说明 |
|---|---|---|
| 音频 | ❌ | 只在内存环形缓冲，从不写文件 |
| 识别文本 | ✅ 默认 / 可关 | `--no-store-text` → `text` 存 NULL，只留 `chars` |
| 耗时与结果 | ✅ | 不含文本，用于统计与 SLO |
| 日志 | — | **仍然禁止**记录全文，只记长度与 outcome（见 `AGENTS.md`） |

关掉文本后统计功能不受影响（`stats()` 只用 `outcome/chars/耗时`）。

删除全部历史：`sqlite3 voiceime.db "DELETE FROM history"` 或删库文件（下次启动自动重建）。
GUI 的「统计」页用 `Store.prune(keep_days)` 做「保留 N 天」。

## 与纠错层的协作

```
ASR 原文 → postprocess.process →（可选）correction.correct + 守卫 → normalize → 注入
                                        │
                                        └─ 采纳与否只写进 Trace（fixes / ms_correct），不落文本
```

- 数据库**不参与**纠错：模型与守卫都是无状态的，没有可编辑的词表
- 历史里的 `text` 是**注入后的最终文本**，`clean_text` 是纠错前的清理结果，
  `raw_text` 是 ASR 原文；三级对照都受 `--no-store-text` 约束
- 旧库的 `terms` 表保留但不读不写（升级不丢用户数据）

## CLI

```bash
uv run python run_spike.py --stats                    # 次数 / 成功率 / P50 / P95
uv run python run_spike.py --history 50               # 最近 50 条
uv run python run_spike.py --web --tab config          # Web 界面：五页，纠错开关在配置页
uv run python run_spike.py --no-db --text "呃 你好"    # 完全不碰数据库
```

冻结形态（`VoiceIME.exe`）参数完全一致，库同样落 `%LOCALAPPDATA%\VoiceIME\voiceime.db`
（该目录不可写时才回退 `<exe 同级>\data`）。

## 并发

单进程写、`WAL` + `busy_timeout=3000`。托盘进程与 GUI 进程会同时打开同一个库：

- 读不阻塞写（WAL），GUI 每 5 秒轮询一次新记录
- 写冲突最多等 3s，仍失败就抛 `sqlite3.OperationalError`，由调用方决定是否放弃这一次写
- **不要**把同一个 `Store` 实例跨线程用（`sqlite3` 默认 `check_same_thread=True`）
- GUI 的写操作只有设置项；历史不提供编辑（用户看到的就是注入过的）

`store.py` 的写操作都是"一条语句 + commit"，没有跨语句事务，
因此不会出现半条历史。
