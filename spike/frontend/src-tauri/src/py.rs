//! Python 核心子进程管理（NDJSON over stdio）。
//!
//! 协议契约见 `docs/03-core-concepts/ipc-contract.md`，本文件的实现 MUST 与之一致：
//!
//! ```text
//! 请求  {"id":1,"method":"bootstrap","params":{}}
//! 响应  {"id":1,"ok":true,"result":{...}} / {"id":1,"ok":false,"error":"..."}
//! 通知  {"event":"ready","result":{...}}          （无 id，Python 主动推送）
//! ```
//!
//! 三条硬约束：
//! 1. stdout 是协议专用通道，只按行解析 JSON；非 JSON 行只记日志、不让进程崩。
//! 2. 等 `{"event":"ready"}` 最多 10 秒，超时即判启动失败。
//! 3. 退出 MUST 先发 `shutdown`，给 Python 自净的时间，再 `kill()` 兜底并 `wait()` 回收，
//!    不留僵尸（见 [`PythonBridge::shutdown`]）。

use std::collections::HashMap;
use std::env;
use std::ffi::OsString;
use std::io::{self, BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};

/// 等 Python 核 `ready` 通知的上限（契约规定 10s）。
pub const READY_TIMEOUT: Duration = Duration::from_secs(10);
/// 单次 RPC 的上限。`check_update` 之类会走网络，留够余量。
pub const CALL_TIMEOUT: Duration = Duration::from_secs(30);
/// 退出时给 `shutdown` 方法的应答时间。
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(3);
/// `shutdown` 之后等进程自己退出的时间，超时才动 `kill()`。
const REAP_TIMEOUT: Duration = Duration::from_secs(2);

/// 前端监听：Python 核异常退出（以及正常退出），payload 形如 `{"code":0}`。
pub const EVENT_EXIT: &str = "python-exit";
/// 前端监听：Python 核就绪。
pub const EVENT_READY: &str = "python-ready";
/// 前端监听：Python 主动推送的通知（`{"event":"log",...}` 等），payload 即原消息。
pub const EVENT_NOTIFY: &str = "python-event";

/// 子进程状态机。`Exited` 之后所有 `py_call` 立即失败，不再干等超时。
#[derive(Debug)]
enum Status {
    Starting,
    Ready,
    Exited(Option<i32>),
    Failed(String),
}

impl Status {
    fn is_terminal(&self) -> bool {
        matches!(self, Status::Exited(_) | Status::Failed(_))
    }
}

/// 读线程、`shutdown()`、`Drop` 三方共享的可变状态。
///
/// 锁顺序统一为 `status -> pending`（`child` / `stdin` 是叶子锁，任何时候都不回指），
/// 这样 `call()` 与退出路径不会互相等死。
struct Inner {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    /// 发出但还没等到响应的调用：`id -> 应答通道`。
    pending: Mutex<HashMap<u64, Sender<Result<Value, String>>>>,
    status: Mutex<Status>,
    status_cv: Condvar,
    app: Mutex<Option<AppHandle>>,
    /// `python-exit` 只发一次（读线程与 kill 兜底路径可能同时到达）。
    exit_reported: AtomicBool,
}

impl Inner {
    fn emit(&self, event: &str, payload: impl Serialize + Clone) {
        let app = self.app.lock().unwrap().clone();
        if let Some(app) = app {
            let _ = app.emit(event, payload);
        }
    }

    fn write_line(&self, line: &str) -> io::Result<()> {
        let mut slot = self.stdin.lock().unwrap();
        let stdin = slot.as_mut().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotConnected, "Python 核的 stdin 已关闭")
        })?;
        stdin.write_all(line.as_bytes())?;
        stdin.write_all(b"\n")?;
        stdin.flush()
    }

    fn close_stdin(&self) {
        // 丢掉 ChildStdin 即关闭管道；Python 侧会读到 EOF（契约的另一个正常退出条件）。
        drop(self.stdin.lock().unwrap().take());
    }

    fn mark_ready(&self) {
        {
            let mut st = self.status.lock().unwrap();
            if matches!(&*st, Status::Starting) {
                *st = Status::Ready;
            }
        }
        self.status_cv.notify_all();
    }

    fn mark_failed(&self, msg: String) {
        {
            let mut st = self.status.lock().unwrap();
            if !st.is_terminal() {
                *st = Status::Failed(msg);
            }
        }
        self.status_cv.notify_all();
    }

    /// 记录退出并唤醒所有等待者。返回 `true` 表示本次是第一个上报者（由它发事件）。
    ///
    /// **锁纪律：这里绝不嵌套持锁。** `rpc()` 是按 `status -> pending` 取的，
    /// 本函数若在 `status` 里再去拿 `pending`，读线程上报退出时就会和一条
    /// 正在登记的调用撞成 AB-BA 死锁（窗口关了壳却不退）。
    /// 所以先放掉 `status`，再独立给 `pending` 上锁。
    fn mark_exited(&self, code: Option<i32>) -> bool {
        let first = {
            let mut st = self.status.lock().unwrap();
            let first = !st.is_terminal();
            if first {
                *st = Status::Exited(code);
            }
            first
        };
        self.status_cv.notify_all();

        // 排空 pending：还在等的调用方要立刻拿到错误，而不是干等 30s 超时。
        let waiters: Vec<Sender<Result<Value, String>>> = {
            let mut pending = self.pending.lock().unwrap();
            pending.drain().map(|(_, tx)| tx).collect()
        };
        if !waiters.is_empty() {
            let err = format!("Python 核已退出（code: {code:?}）");
            for tx in waiters {
                let _ = tx.send(Err(err.clone()));
            }
        }
        first
    }

    /// 收尸：`wait()` 之后再取退出码，避免僵尸进程。
    fn reap(&self) -> Option<i32> {
        let mut slot = self.child.lock().unwrap();
        match slot.take() {
            Some(mut child) => child.wait().ok().and_then(|s| s.code()),
            // 已经被 `shutdown()` / `Drop` 收走，说明退出码已由那条路径上报。
            None => None,
        }
    }
}

/// Python 核的句柄：spawn、转发调用、退出时收尾。
pub struct PythonBridge {
    inner: Arc<Inner>,
    next_id: AtomicU64,
    started: AtomicBool,
    shutdown_done: AtomicBool,
}

impl PythonBridge {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Inner {
                child: Mutex::new(None),
                stdin: Mutex::new(None),
                pending: Mutex::new(HashMap::new()),
                status: Mutex::new(Status::Starting),
                status_cv: Condvar::new(),
                app: Mutex::new(None),
                exit_reported: AtomicBool::new(false),
            }),
            next_id: AtomicU64::new(0),
            started: AtomicBool::new(false),
            shutdown_done: AtomicBool::new(false),
        }
    }

    /// 拉起 Python 核并等它报 `ready`。同步阻塞，最多 [`READY_TIMEOUT`]。
    ///
    /// 失败（python 不在 PATH、脚本找不到、10s 超时）时会把状态钉在 `Failed`，
    /// 之后的 `py_call` 直接返回这条错误，前端据此显示横幅。
    pub fn start(&self, app: AppHandle) -> Result<(), String> {
        if self.started.swap(true, Ordering::SeqCst) {
            return Err("Python 核已经启动过，不允许二次 start".to_string());
        }
        *self.inner.app.lock().unwrap() = Some(app);

        let python = python_exe();
        let dir = core_dir();
        let db = env::var("VOICEIME_DB").ok().filter(|s| !s.trim().is_empty());

        let mut cmd = Command::new(&python);
        cmd.arg("run_spike.py").arg("--ipc");
        if let Some(db) = db.as_deref() {
            cmd.arg("--db").arg(db);
        }
        cmd.current_dir(&dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            // 行缓冲 + 固定 UTF-8：NDJSON 一行一条，缓冲会把协议憋住；
            // 编码不固定时中文错误信息可能变 GBK，前端解析必炸。
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8");

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // 0x0800_0000 = CREATE_NO_WINDOW：release 版是无控制台子系统，
            // 不加这个每次启动都会闪一个黑框。
            cmd.creation_flags(0x0800_0000);
        }

        let mut child = cmd.spawn().map_err(|e| {
            let msg = format!(
                "启动 Python 核失败：{} run_spike.py --ipc（工作目录 {}）：{e}",
                python.to_string_lossy(),
                dir.display()
            );
            eprintln!("[shell] {msg}");
            self.inner.mark_failed(msg.clone());
            msg
        })?;

        let pid = child.id();
        let (stdin, stdout, stderr) =
            match (child.stdin.take(), child.stdout.take(), child.stderr.take()) {
                (Some(i), Some(o), Some(e)) => (i, o, e),
                _ => {
                    let _ = child.kill();
                    let _ = child.wait();
                    let msg = "Python 核的 stdio 管道没建立起来（内部错误）".to_string();
                    self.inner.mark_failed(msg.clone());
                    return Err(msg);
                }
            };

        eprintln!(
            "[shell] 已拉起 Python 核 pid={pid:?}，工作目录 {}",
            dir.display()
        );

        *self.inner.stdin.lock().unwrap() = Some(stdin);
        *self.inner.child.lock().unwrap() = Some(child);

        spawn_stdout_reader(Arc::clone(&self.inner), stdout);
        spawn_stderr_reader(stderr);

        self.wait_ready()
    }

    fn wait_ready(&self) -> Result<(), String> {
        let (guard, _) = self
            .inner
            .status_cv
            .wait_timeout_while(
                self.inner.status.lock().unwrap(),
                READY_TIMEOUT,
                |s| matches!(s, Status::Starting),
            )
            .expect("Python 核状态锁被 poison");

        match &*guard {
            Status::Ready => Ok(()),
            Status::Failed(msg) => Err(msg.clone()),
            Status::Exited(code) => Err(format!("Python 核在 ready 之前就退出了（code: {code:?}）")),
            Status::Starting => {
                drop(guard);
                let msg = format!(
                    "等 Python 核 ready 超时（{}s），请检查 VOICEIME_PYTHON 与 run_spike.py --ipc",
                    READY_TIMEOUT.as_secs()
                );
                eprintln!("[shell] {msg}");
                self.inner.mark_failed(msg.clone());
                self.kill_and_reap();
                Err(msg)
            }
        }
    }

    /// 发一条请求并等它的响应。
    pub fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        self.rpc(method, params, CALL_TIMEOUT)
    }

    fn rpc(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed) + 1;
        let (tx, rx) = mpsc::channel();

        // 登记 pending 与检查状态在同一把锁下完成：否则「刚判完 Ready 就退出」
        // 会让这条请求既没人应答、也没被退出路径排空，白白等满超时。
        {
            let status = self.inner.status.lock().unwrap();
            match &*status {
                Status::Ready => {}
                Status::Exited(code) => {
                    return Err(format!("Python 核已退出（code: {code:?}）"));
                }
                Status::Failed(msg) => return Err(msg.clone()),
                Status::Starting => return Err("Python 核尚未就绪".to_string()),
            }
            self.inner.pending.lock().unwrap().insert(id, tx);
        }

        let line = serde_json::to_string(&json!({
            "id": id,
            "method": method,
            "params": params,
        }))
        .map_err(|e| format!("序列化 {method} 的请求失败：{e}"))?;

        if let Err(e) = self.inner.write_line(&line) {
            self.inner.pending.lock().unwrap().remove(&id);
            return Err(format!("写入 Python 核失败：{e}"));
        }

        match rx.recv_timeout(timeout) {
            Ok(res) => res,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                self.inner.pending.lock().unwrap().remove(&id);
                Err(format!(
                    "调用 {method} 超时（{}s）",
                    timeout.as_secs_f32().round() as u64
                ))
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                Err(format!("调用 {method} 失败：应答通道已断，Python 核已退出"))
            }
        }
    }

    /// 先礼后兵地结束子进程，可重入（`RunEvent::Exit` 与 `Drop` 都会来一次）。
    ///
    /// 1. 发 `shutdown`，让 Python 自己收尾（关麦克风、提交数据库）；
    /// 2. 关 stdin，`shutdown` 没应答时 Python 也能靠 EOF 退出主循环；
    /// 3. 最多等 [`REAP_TIMEOUT`]；还不退就 `kill()` + `wait()` 收尸。
    pub fn shutdown(&self) {
        if !self.started.load(Ordering::SeqCst) || self.shutdown_done.swap(true, Ordering::SeqCst) {
            return;
        }

        // 1) 先礼：正常路径下 Python 收到 shutdown 会自己退出。
        //    已经退出/未就绪时这里立刻返回错误，忽略即可。
        match self.rpc("shutdown", Value::Null, SHUTDOWN_TIMEOUT) {
            Ok(v) => eprintln!("[shell] Python 核 shutdown 应答：{v}"),
            Err(e) => eprintln!("[shell] shutdown 应答未拿到（{e}），继续按进程状态收尾"),
        }

        // 2) 兜底通道：关 stdin 让 Python 读到 EOF。
        self.inner.close_stdin();

        // 3) 给它一点时间自己退，别一上来就 kill（丢数据库写入）。
        if self.wait_exited(REAP_TIMEOUT) {
            eprintln!("[shell] Python 核已自行退出");
            return;
        }

        eprintln!("[shell] Python 核 {}s 内未退出，强制 kill", REAP_TIMEOUT.as_secs());
        self.kill_and_reap();
    }

    fn wait_exited(&self, timeout: Duration) -> bool {
        let (_guard, _) = self
            .inner
            .status_cv
            .wait_timeout_while(self.inner.status.lock().unwrap(), timeout, |s| {
                !s.is_terminal()
            })
            .expect("Python 核状态锁被 poison");
        self.inner.status.lock().unwrap().is_terminal()
    }

    /// 强杀并收尸，无论成败都把状态推进到 `Exited`，保证不会有僵尸。
    ///
    /// 同样不嵌套持锁：`child` / `stdin` / `status` 各自独立进出。
    fn kill_and_reap(&self) {
        let taken = {
            let mut slot = self.inner.child.lock().unwrap();
            slot.take()
        };
        let Some(mut child) = taken else {
            return;
        };
        if let Err(e) = child.kill() {
            eprintln!("[shell] kill Python 核失败（可能已自行退出）：{e}");
        }
        let code = child.wait().ok().and_then(|s| s.code());
        self.inner.close_stdin();
        self.report_exit(code);
    }

    fn report_exit(&self, code: Option<i32>) {
        if !self.inner.mark_exited(code) {
            return;
        }
        if self.inner.exit_reported.swap(true, Ordering::SeqCst) {
            return;
        }
        eprintln!("[shell] Python 核退出，code={code:?}");
        self.inner.emit(EVENT_EXIT, ExitPayload { code });
    }
}

impl Drop for PythonBridge {
    fn drop(&mut self) {
        // 兜底：没走过 `RunEvent::Exit`（比如 panic 展开）时也别把子进程漏在外面。
        if self.started.load(Ordering::SeqCst) && !self.shutdown_done.load(Ordering::SeqCst) {
            eprintln!("[shell] PythonBridge 被 Drop 且未 shutdown，直接强杀子进程");
            self.kill_and_reap();
        }
    }
}

impl Default for PythonBridge {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Serialize)]
struct ExitPayload {
    code: Option<i32>,
}

/// 读 stdout：逐行解析 NDJSON，按 `id` 唤醒等待方，`event` 转成 Tauri 事件。
fn spawn_stdout_reader(inner: Arc<Inner>, stdout: ChildStdout) {
    thread::Builder::new()
        .name("py-stdout".to_string())
        .spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let line = match line {
                    Ok(l) => l,
                    Err(e) => {
                        // 常用错因：Python 侧输出非 UTF-8 字节。协议已污染，但别让壳崩。
                        eprintln!("[py:stdout] 读行失败：{e}");
                        break;
                    }
                };
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                let msg: Value = match serde_json::from_str(line) {
                    Ok(v) => v,
                    Err(e) => {
                        eprintln!("[py:stdout] 非 JSON 行（stdout 被日志污染？）：{e} | {line}");
                        continue;
                    }
                };

                // 响应：按 id 交回等待方。
                if let Some(id) = msg.get("id").and_then(Value::as_u64) {
                    let waiter = inner.pending.lock().unwrap().remove(&id);
                    let res = if msg.get("ok").and_then(Value::as_bool).unwrap_or(false) {
                        Ok(msg.get("result").cloned().unwrap_or(Value::Null))
                    } else {
                        Err(msg
                            .get("error")
                            .and_then(Value::as_str)
                            .unwrap_or("Python 核返回 ok=false 但没给 error")
                            .to_string())
                    };
                    match waiter {
                        Some(tx) => {
                            // 接收端可能已超时放弃，send 失败属正常。
                            let _ = tx.send(res);
                        }
                        None => eprintln!("[py:stdout] 收到未知 id={id} 的响应，已丢弃"),
                    }
                    continue;
                }

                // 通知：ready 单独处理，其余原样转给前端。
                let event = msg.get("event").and_then(Value::as_str).unwrap_or("");
                match event {
                    "ready" => {
                        inner.mark_ready();
                        inner.emit(EVENT_READY, msg.get("result").cloned().unwrap_or(Value::Null));
                    }
                    "" => eprintln!("[py:stdout] 既无 id 也无 event，已丢弃：{line}"),
                    _ => inner.emit(EVENT_NOTIFY, msg.clone()),
                }
            }

            // stdout EOF ⇒ 进程正在退出。收尸后上报（若 kill 路径已收过则不重复发）。
            let code = inner.reap();
            if !inner.mark_exited(code) {
                return;
            }
            if inner.exit_reported.swap(true, Ordering::SeqCst) {
                return;
            }
            eprintln!("[py:stdout] Python 核 stdout 关闭，code={code:?}");
            inner.emit(EVENT_EXIT, ExitPayload { code });
        })
        .expect("无法创建 py-stdout 线程");
}

/// stderr 全量转到壳的日志（debug 构建有控制台，release 走宿主 stderr）。
/// 契约要求 Python 的日志/print 都走 stderr——排障时这里是唯一现场。
fn spawn_stderr_reader(stderr: ChildStderr) {
    thread::Builder::new()
        .name("py-stderr".to_string())
        .spawn(move || {
            for line in BufReader::new(stderr).lines() {
                match line {
                    Ok(l) => eprintln!("[py:stderr] {l}"),
                    Err(e) => {
                        eprintln!("[py:stderr] 读行失败：{e}");
                        break;
                    }
                }
            }
        })
        .expect("无法创建 py-stderr 线程");
}

/// Python 解释器路径：`VOICEIME_PYTHON` 优先，默认 `python`。
fn python_exe() -> OsString {
    env::var_os("VOICEIME_PYTHON")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| OsString::from("python"))
}

/// Python 核心所在目录（`run_spike.py` 的父目录）。
///
/// 优先 `VOICEIME_CORE_DIR`；打包态找 exe 同级 `python/`；开发态从
/// `CARGO_MANIFEST_DIR` 往上找第一个含 `run_spike.py` 的目录——这样壳放在
/// `spike/src-tauri` 还是 `spike/frontend/src-tauri` 都不用改代码。
fn core_dir() -> PathBuf {
    if let Some(dir) = env::var_os("VOICEIME_CORE_DIR") {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }

    if let Ok(exe) = env::current_exe() {
        if let Some(dir) = exe.parent() {
            let candidates = [
                dir.join("python"),
                dir.join("resources").join("python"),
                dir.to_path_buf(),
            ];
            for cand in candidates {
                if cand.join("run_spike.py").is_file() {
                    return cand;
                }
            }
        }
    }

    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    for anc in manifest_dir.ancestors() {
        if anc.join("run_spike.py").is_file() {
            return anc.to_path_buf();
        }
    }
    let fallback = manifest_dir.join("..").join("..");
    fallback.canonicalize().unwrap_or_else(|_| fallback)
}
