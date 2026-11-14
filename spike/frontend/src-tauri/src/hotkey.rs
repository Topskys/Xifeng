//! 按住说话（PTT）的全局按键采集：`WH_KEYBOARD_LL` 低级键盘钩子。
//!
//! 这是 `voiceime_spike/hotkey.py` 里 `LowLevelHook` 的等价实现（D16：
//! 壳接管按键采集）。为什么不用 `RegisterHotKey`：它只报"按下"，拿不到
//! "松开"，而 PTT 必须要 KeyUp。为什么必须低级钩子：本机实测
//! `GetAsyncKeyState` 读不到按键（G18/G19）。
//!
//! 线程模型：钩子必须装在**有消息泵**的线程上，所以单开一个线程跑
//! `GetMessageW` 循环；回调只往 down 集合里增删，不做任何耗时操作。
//!
//! 隐私：钩子会看到**所有**按键，但代码只把 vkCode 与白名单比对，
//! 不记录、不落盘、不外发。
//!
//! 只忽略带 [`EXTRA_INFO_MAGIC`] 的注入事件（即本程序自己打的字），
//! 其它 injected 事件照收 —— 键盘重映射软件产生的真实按键也是 injected，
//! 一律忽略会让 PTT 在那些机器上完全失灵（G20）。

use std::collections::HashSet;
use std::sync::atomic::{AtomicI32, AtomicU32, AtomicU64, AtomicBool, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::Duration;

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::Threading::GetCurrentThreadId;
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetMessageW, PeekMessageW, PostThreadMessageW,
    SetWindowsHookExW, UnhookWindowsHookEx, HHOOK, KBDLLHOOKSTRUCT, MSG,
    PM_NOREMOVE, WH_KEYBOARD_LL, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP,
};

pub use crate::inject::EXTRA_INFO_MAGIC;

// 键位白名单（与 hotkey.py 的 KEYS 一致）
pub const VK_ESCAPE: u32 = 0x1B;
pub const VK_LSHIFT: u32 = 0xA0;
pub const VK_RSHIFT: u32 = 0xA1;
pub const VK_LCONTROL: u32 = 0xA2;
pub const VK_RCONTROL: u32 = 0xA3;


const LLKHF_INJECTED: u32 = 0x10;
const WM_QUIT: u32 = 0x0012;

/// PTT 状态机轮询周期（与 Python `DEFAULT_POLL_MS` 一致）。
pub const POLL_MS: u64 = 10;

/// 键位预设：vk 组合（与 hotkey.py 的 KEYS 一致）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct KeyPreset(&'static [u32]);

pub const KEY_CTRL: KeyPreset = KeyPreset(&[VK_LCONTROL, VK_RCONTROL]);
pub const KEY_LCTRL: KeyPreset = KeyPreset(&[VK_LCONTROL]);
pub const KEY_RCTRL: KeyPreset = KeyPreset(&[VK_RCONTROL]);
pub const KEY_SHIFT: KeyPreset = KeyPreset(&[VK_LSHIFT, VK_RSHIFT]);
pub const KEY_LSHIFT: KeyPreset = KeyPreset(&[VK_LSHIFT]);
pub const KEY_RSHIFT: KeyPreset = KeyPreset(&[VK_RSHIFT]);

impl KeyPreset {
    pub fn vks(self) -> &'static [u32] {
        self.0
    }

    /// 从设置里的 `hotkey` 字段解析；未知取值退回默认 ctrl（与 Python 一致）。
    pub fn parse(s: &str) -> Self {
        match s {
            "lctrl" => KEY_LCTRL,
            "rctrl" => KEY_RCTRL,
            "shift" => KEY_SHIFT,
            "lshift" => KEY_LSHIFT,
            "rshift" => KEY_RSHIFT,
            _ => KEY_CTRL,
        }
    }
}

// 钩子回调拿不到上下文指针（LL 钩子不传用户数据），与 Python 的
// 闭包绑定对应：这里用进程级单例。同一时刻只需要一个采集钩子。
static HOOK_STATE: OnceLock<Arc<HookShared>> = OnceLock::new();

#[derive(Debug)]
pub struct HookShared {
    /// 当前按下的 vk 集合（回调线程写，状态机线程读）。
    down: Mutex<HashSet<u32>>,
    /// 自上次 drain 以来新按下的 vk（不漏极短敲击，组合键判定靠它）。
    new_presses: Mutex<Vec<u32>>,
    total: AtomicU64,
    injected: AtomicU64,
    tid: AtomicU32,
    active: AtomicBool,
    error: AtomicI32,
}

impl HookShared {
    /// 失败实例：active=false、error 带错误码（调用方据此降级/报诊断）。
    fn inert(code: i32) -> Arc<Self> {
        Arc::new(Self {
            down: Mutex::new(HashSet::new()),
            new_presses: Mutex::new(Vec::new()),
            total: AtomicU64::new(0),
            injected: AtomicU64::new(0),
            tid: AtomicU32::new(0),
            active: AtomicBool::new(false),
            error: AtomicI32::new(code),
        })
    }

    fn pressed(&self, vks: &[u32]) -> bool {
        let down = self.down.lock().unwrap();
        vks.iter().any(|vk| down.contains(vk))
    }

    /// 取走自上次调用以来新按下的 vk 列表（不漏极短的敲击）。
    fn drain_presses(&self) -> Vec<u32> {
        std::mem::take(&mut *self.new_presses.lock().unwrap())
    }
}

/// 低级键盘钩子的持有者：`start()` 起线程装钩子，`stop()` 发 WM_QUIT 收线程。
pub struct Hook {
    shared: Arc<HookShared>,
    // P2 接 Trace/优雅退出时用（进程退出即随钩子线程消亡，当前无需读）
    #[allow(dead_code)]
    thread: Mutex<Option<thread::JoinHandle<()>>>,
}

impl Hook {
    /// 启动钩子线程并等它就绪（超时 2s，与 Python 一致）。
    /// 总是返回实例：装不上时 `active()` 为 false、`error()` 带错误码
    ///（与 Python `LowLevelHook.start()` 的"对象存在 + active 标志"同语义），
    /// 调用方据此决定是否起语音编排。
    pub fn start() -> Arc<Self> {
        let (tx, rx) = channel();
        let spawned = thread::Builder::new()
            .name("ime-keyhook".into())
            .spawn(move || hook_thread(tx))
            .ok();

        let shared = match rx.recv_timeout(Duration::from_secs(2)) {
            Ok(Ok(shared)) => shared,
            Ok(Err(code)) => {
                eprintln!("[hotkey] SetWindowsHookExW 失败（error={code}）——\
                           全局钩子收不到按键（G21：Low 完整性 / 安全软件拦截）");
                HookShared::inert(code)
            }
            Err(_) => {
                eprintln!("[hotkey] 钩子线程 2s 内未就绪，按失败处理");
                HookShared::inert(-1)
            }
        };
        Arc::new(Hook { shared, thread: Mutex::new(spawned) })
    }

    pub fn active(&self) -> bool {
        self.shared.active.load(Ordering::SeqCst)
    }

    /// SetWindowsHookExW 的错误码（诊断用）。
    pub fn error(&self) -> i32 {
        self.shared.error.load(Ordering::SeqCst)
    }

    /// 任意一个 vk 处于按下状态。
    pub fn pressed(&self, vks: &[u32]) -> bool {
        self.shared.pressed(vks)
    }

    pub fn drain_presses(&self) -> Vec<u32> {
        self.shared.drain_presses()
    }

    /// 收到的按键事件总数（诊断用，能区分"没按键"与"收不到事件"）。
    /// P2 接 Trace 时接线；进程退出即随钩子线程消亡，暂无调用方。
    #[allow(dead_code)]
    pub fn event_count(&self) -> u64 {
        self.shared.total.load(Ordering::Relaxed)
    }

    /// 其中来自注入（LLKHF_INJECTED）的事件数。
    /// 键盘重映射软件产生的真实按键也是 injected，所以这个数字
    /// 不为 0 不等于"没有物理按键"（G20）。
    #[allow(dead_code)] // P2 接 Trace 时接线
    pub fn injected_count(&self) -> u64 {
        self.shared.injected.load(Ordering::Relaxed)
    }

    /// 停止钩子并收线程（PostThreadMessage WM_QUIT）。
    /// 进程退出即随钩子线程消亡；P2 做优雅退出时接线。
    #[allow(dead_code)]
    pub fn stop(&self) {
        let tid = self.shared.tid.load(Ordering::SeqCst);
        if tid != 0 {
            unsafe {
                let _ = PostThreadMessageW(tid, WM_QUIT, WPARAM(0), LPARAM(0));
            }
        }
        if let Some(h) = self.thread.lock().unwrap().take() {
            let _ = h.join();
        }
    }
}

unsafe extern "system" fn hook_proc(n_code: i32, w_param: WPARAM, l_param: LPARAM) -> LRESULT {
    if n_code == 0 {
        let info = &*(l_param.0 as *const KBDLLHOOKSTRUCT);
        let injected = (info.flags.0 & LLKHF_INJECTED) != 0;
        // 只忽略**自己**注入的事件（带 MAGIC），而不是所有 injected 事件：
        // 键盘重映射软件产生的真实按键也是 injected，一律忽略会让 PTT 失灵（G20）
        let ours = info.dwExtraInfo == EXTRA_INFO_MAGIC as usize;
        if let Some(shared) = HOOK_STATE.get() {
            if !ours {
                let vk = info.vkCode;
                let msg = w_param.0 as u32;
                if msg == WM_KEYDOWN || msg == WM_SYSKEYDOWN {
                    let mut down = shared.down.lock().unwrap();
                    down.insert(vk);
                    shared.new_presses.lock().unwrap().push(vk);
                    shared.total.fetch_add(1, Ordering::Relaxed);
                    if injected {
                        shared.injected.fetch_add(1, Ordering::Relaxed);
                    }
                } else if msg == WM_KEYUP || msg == WM_SYSKEYUP {
                    shared.down.lock().unwrap().remove(&vk);
                    shared.total.fetch_add(1, Ordering::Relaxed);
                    if injected {
                        shared.injected.fetch_add(1, Ordering::Relaxed);
                    }
                }
            }
        }
    }
    CallNextHookEx(HHOOK::default(), n_code, w_param, l_param)
}

fn hook_thread(ready: Sender<Result<Arc<HookShared>, i32>>) {
    let shared = Arc::new(HookShared {
        down: Mutex::new(HashSet::new()),
        new_presses: Mutex::new(Vec::new()),
        total: AtomicU64::new(0),
        injected: AtomicU64::new(0),
        tid: AtomicU32::new(0),
        active: AtomicBool::new(false),
        error: AtomicI32::new(0),
    });
    HOOK_STATE
        .set(Arc::clone(&shared))
        .expect("键盘钩子状态只允许初始化一次");

    let proc = hook_proc as unsafe extern "system" fn(i32, WPARAM, LPARAM) -> LRESULT;
    let tid = unsafe { GetCurrentThreadId() };
    shared.tid.store(tid, Ordering::SeqCst);

    unsafe {
        // 先强制创建本线程的消息队列，否则 LL 钩子可能一个事件都收不到
        let mut msg = MSG::default();
        let _ = PeekMessageW(&mut msg, HWND::default(), 0, 0, PM_NOREMOVE);
        // LL 钩子的回调在本进程内执行，hMod 必须传 NULL
        //（传 EXE 句柄会 ERROR_MOD_NOT_FOUND，Python 版同款注释）
        let hook = match SetWindowsHookExW(WH_KEYBOARD_LL, Some(proc), None, 0) {
            Ok(h) => h,
            Err(e) => {
                shared.error.store(e.code().0, Ordering::SeqCst);
                let _ = ready.send(Err(e.code().0));
                return;
            }
        };

        shared.active.store(true, Ordering::SeqCst);
        let _ = ready.send(Ok(Arc::clone(&shared)));

        // 消息泵：钩子线程的宿命循环
        let mut msg = MSG::default();
        while GetMessageW(&mut msg, HWND::default(), 0, 0).as_bool() {
            let _ = DispatchMessageW(&msg);
        }

        let _ = UnhookWindowsHookEx(hook);
        shared.active.store(false, Ordering::SeqCst);
    }
}
