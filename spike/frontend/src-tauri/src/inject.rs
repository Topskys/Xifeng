//! 光标注入：Windows SendInput + KEYEVENTF_UNICODE，剪贴板兜底。
//!
//! **这是 `spike/xifeng/inject.py` 的 Rust 等价实现**：语义、常量、
//! 每个 sleep、每个失败路径都逐段对齐 —— Python 版是大量实测踩坑打磨出来的
//! （G2/G3/G6/G7/G13/G20，编号见 `docs/05-gotchas.md`）。改这里之前先读
//! Python 原文；任何行为变更必须两边同时改。
//!
//! 为什么必须逐分支对齐：
//! - G2 注入按 UTF-16 码元发，emoji 拆错会乱码；
//! - G3 剪贴板恢复太早，慢应用会把"原内容"粘出来；
//! - G6 直接 SetForegroundWindow 会随机失败，必须用 ALT 技巧；
//! - G7 提权窗口会静默吞键，必须显式失败，禁止"看起来成功了"；
//! - G13 令牌/文本读不到时不能武断拒绝，也不能盲目兜底；
//! - G20 钩子只忽略带 EXTRA_INFO_MAGIC 的"自己打的字"。
//!
//! 契约与 spec.md §4.4 一致：
//!     inject_ex(text, snap, cfg) -> InjectResult（带实际用的策略，写 Trace 用）
//!
//! 为什么默认用 UNICODE 而不是剪贴板：
//! - 中文不需要切换输入法，不受输入法状态影响
//! - 天然落在当前 caret，不覆盖用户剪贴板
//! 代价：部分应用（提权窗口、DirectInput 游戏、少数终端）会静默吞掉按键 —— 所以
//! 注入前必须校验焦点与提权状态，失败要显式返回，禁止"看起来成功了"。
//!
//! 剪贴板兜底（`strategy="clipboard"` / `"auto"`）：
//! - 保存**整个剪贴板**（能复制的 HGLOBAL 型格式全部快照）→ 写入文本 → Ctrl+V → 恢复
//! - 粘贴后如果发现剪贴板已被别的程序改写，就放弃恢复，绝不覆盖用户新复制的内容
//! - `auto`：先发 Unicode；只有焦点控件**可被探测**（Edit/RichEdit/Scintilla）且文本
//!   确实没变时，才改走剪贴板。探测不到时保持 Unicode —— 盲目兜底会把同一句话打两遍
//!
//! 与 inject.py 的有意差异（仅语言层）：
//! - `IS_WINDOWS` 在 Rust 里由 `#[cfg(windows)]` 表达：Win32 实现只在 Windows 编译，
//!   非 Windows 提供 stub（返回 Failed/None），与 Python 的运行时判断语义一致；
//! - 日志走 `eprintln!`（Python 是 `print(..., flush=True)`）；
//! - Python 的 try/except 换成显式 `Option`/`Result` 传播，失败语义不变；
//! - `InjectStrategy` 只有三态枚举，`parse` 对未知值按 `Auto` 处理 —— Python 侧
//!   「未知策略显式 Failed」分支依赖原始字符串，在固定枚举签名上无法表达。

// ---------------------------------------------------------------- 常量

/// 注入事件的签名，写进 KEYBDINPUT.dwExtraInfo。
/// 用途：低级键盘钩子能从 KBDLLHOOKSTRUCT.dwExtraInfo 读回它，从而精确忽略"自己打的字"，
/// 而不是一律忽略 LLKHF_INJECTED —— 键盘重映射软件（PowerToys / 厂商驱动）产生的
/// 输入同样是 injected，一律忽略会导致 PTT 在那些机器上完全失灵（G20）。
/// 'VIME' 的 ASCII 值，别改，改了要和 hotkey.py 同步。
pub const EXTRA_INFO_MAGIC: usize = 0x5649_4D45;

const VK_RETURN: u16 = 0x0D;
const VK_MENU: u16 = 0x12;
const VK_CONTROL: u16 = 0x11;
const VK_V: u16 = 0x56;
const VK_TAB: u16 = 0x09;

const CF_UNICODETEXT: u32 = 13;

/// 每批 32 个 UTF-16 码元（spec §4.4）
pub const CHUNK_UNITS: usize = 32;
pub const CHUNK_DELAY_MS: u64 = 5;

// 能被 WM_GETTEXTLENGTH 读出长度的控件。WinForms 的 Edit 类名是
// `WindowsForms10.EDIT.app.0.xxx`，不是裸 "Edit"，只做等值匹配会永远探测不到。
const EDIT_CLASSES: [&str; 5] = ["Edit", "RICHEDIT", "RichEdit20W", "RichEdit50W", "Scintilla"];
const EDIT_CLASS_PREFIXES: [&str; 2] = ["WindowsForms10.EDIT", "WindowsForms10.RichEdit"];

const CONSOLE_CLASSES: [&str; 3] =
    ["ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty"];

// ---------------------------------------------------------------- 类型

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum InjectOutcome {
    Injected,
    FocusLost,
    ElevatedTarget,
    Failed,
    NoTarget,
}

impl InjectOutcome {
    /// 与 Python `InjectOutcome` 枚举的值一致（写 Trace 用）。
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Injected => "Injected",
            Self::FocusLost => "FocusLost",
            Self::ElevatedTarget => "ElevatedTarget",
            Self::Failed => "Failed",
            Self::NoTarget => "NoTarget",
        }
    }
}

/// 注入事件：Unicode 码元（`Text`）或虚拟键（`Key`）。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Event {
    Text(u16),
    Key(u16),
}

#[derive(Clone, Debug, PartialEq)]
pub struct FocusSnapshot {
    pub hwnd: isize,
    pub pid: u32,
    pub class_name: String,
    /// None = 读不到令牌（受保护进程，G13）
    pub elevated: Option<bool>,
    pub is_console: bool,
}

/// 注入策略：unicode | clipboard | auto。
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum InjectStrategy {
    Unicode,
    Clipboard,
    #[default]
    Auto,
}

impl InjectStrategy {
    /// 解析 Python 侧的策略字符串（大小写不敏感、去首尾空白）。
    /// - 空值按 `Unicode`（Python：`cfg.strategy or "unicode"`）；
    /// - 未知值按 `Auto`（与 `#[default]` 一致）：三态枚举装不下"非法"标记，
    ///   inject.py 的「未知策略显式 Failed」分支无法在枚举签名上表达，
    ///   见模块文档「有意差异」。
    pub fn parse(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "clipboard" => Self::Clipboard,
            "auto" => Self::Auto,
            "" | "unicode" => Self::Unicode,
            _ => Self::Auto,
        }
    }
}

#[derive(Clone, Debug)]
pub struct InjectConfig {
    pub strategy: InjectStrategy,
    /// 焦点丢失时是否用 ALT 技巧抢回
    pub restore_focus: bool,
    pub chunk_units: usize,
    pub chunk_delay_ms: u64,
    pub verify_focus: bool,
    /// 粘完多久再恢复剪贴板。G3：恢复太早，慢应用会把"原内容"粘出来。
    pub clipboard_restore_ms: u64,
}

impl Default for InjectConfig {
    fn default() -> Self {
        Self {
            strategy: InjectStrategy::Auto,
            restore_focus: false,
            chunk_units: CHUNK_UNITS,
            chunk_delay_ms: CHUNK_DELAY_MS,
            verify_focus: true,
            clipboard_restore_ms: 500,
        }
    }
}

/// 实际使用的策略：unicode | clipboard | none
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct InjectResult {
    pub outcome: InjectOutcome,
    pub strategy: &'static str,
}

/// 剪贴板快照。只保存能用 HGLOBAL 复制的格式（文本/HTML/DIB/私有格式）。
#[derive(Debug, Default)]
pub struct ClipboardSnapshot {
    /// 按枚举顺序保存（与 Python dict 的插入序一致）
    pub formats: Vec<(u32, Vec<u8>)>,
    /// CF_BITMAP 等句柄型格式，无法复制
    pub skipped: Vec<u32>,
}

/// 焦点控件文本快照。text=None 表示太长只读了长度。
#[derive(Clone, Debug, PartialEq)]
pub struct FocusText {
    pub length: i32,
    pub text: Option<String>,
}

pub fn is_edit_class(class_name: &str) -> bool {
    EDIT_CLASSES.contains(&class_name)
        || EDIT_CLASS_PREFIXES.iter().any(|p| class_name.starts_with(p))
}

// ---------------------------------------------------------------- 纯逻辑

/// 把文本转成事件序列。
///
/// - 按 UTF-16 码元发送：emoji（😀 = 2 码元）按字符拆会乱码（G2）
///   （Rust 的 `str` 不可能含孤立代理项，`encode_utf16` 与 Python 的
///   `encode("utf-16-le", "surrogatepass")` 对合法文本逐码元等价）
/// - 换行 MUST 用 VK_RETURN，MUST NOT 发 Unicode '\n'（部分应用不认）
/// - '\r\n' 归一成单个换行，避免敲出两个回车
pub fn build_events(text: &str) -> Vec<Event> {
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut events = Vec::new();
    for unit in normalized.encode_utf16() {
        match unit {
            0x0A => events.push(Event::Key(VK_RETURN)),
            0x09 => events.push(Event::Key(VK_TAB)),
            _ => events.push(Event::Text(unit)),
        }
    }
    events
}

/// 按 size 切批。size 为 0 时整段作为一批（与 Python `size <= 0` 分支一致）。
pub fn chunked(events: &[Event], size: usize) -> Vec<Vec<Event>> {
    if size == 0 {
        return if events.is_empty() { Vec::new() } else { vec![events.to_vec()] };
    }
    events.chunks(size).map(<[Event]>::to_vec).collect()
}

/// 两次快照能否断定"什么都没变"。
///
/// 能读到全文时比全文（粘贴替换等长选区也能识别出来）；太长只比长度。
/// 任一端读不到就返回 False —— 探测不到不能兜底，否则同一句话会打两遍。
pub fn definitely_unchanged(before: Option<&FocusText>, after: Option<&FocusText>) -> bool {
    let (Some(before), Some(after)) = (before, after) else {
        return false;
    };
    match (&before.text, &after.text) {
        (Some(bt), Some(at)) => bt == at,
        _ => before.length == after.length,
    }
}

// ---------------------------------------------------------------- Win32

#[cfg(windows)]
mod win32 {
    use super::*;
    use std::time::{Duration, Instant};

    use windows::Win32::Foundation::{
        CloseHandle, GlobalFree, HANDLE, HGLOBAL, HWND, LPARAM, WPARAM,
    };
    use windows::Win32::Security::{
        GetTokenInformation, TokenElevation, TOKEN_ELEVATION, TOKEN_QUERY,
    };
    use windows::Win32::System::DataExchange::{
        CloseClipboard, EmptyClipboard, EnumClipboardFormats, GetClipboardData,
        IsClipboardFormatAvailable, OpenClipboard, SetClipboardData,
    };
    use windows::Win32::System::Memory::{
        GlobalAlloc, GlobalLock, GlobalSize, GlobalUnlock, GMEM_MOVEABLE,
    };
    use windows::Win32::System::Threading::{
        GetCurrentProcessId, OpenProcess, OpenProcessToken, PROCESS_QUERY_INFORMATION,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYBD_EVENT_FLAGS, KEYEVENTF_KEYUP,
        KEYEVENTF_UNICODE, VIRTUAL_KEY,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GetClassNameW, GetForegroundWindow, GetGUIThreadInfo, GetWindowThreadProcessId,
        SendMessageTimeoutW, SetForegroundWindow, GUITHREADINFO, SMTO_ABORTIFHUNG, WM_GETTEXT,
        WM_GETTEXTLENGTH,
    };

    /// 读文本的缓冲上限：超过就只比长度（大文档不值当为一次探测拷几 MB）
    pub(super) const TEXT_PROBE_LIMIT: i32 = 4096;

    const WAIT_FOR_CHANGE_MS: u64 = 120;

    /// `Clipboard::open` 的 Python 默认参数：重试 5 次 × 每次 20ms。
    const OPEN_RETRIES: u32 = 5;
    const OPEN_DELAY_MS: u64 = 20;

    /// 查询目标进程是否提权。
    ///
    /// 返回 None 表示**查不到**（OpenProcess 被拒 = 目标受保护或更高完整性级别）。
    /// None 必须按"有风险"处理：把 None 当 False 会让提权窗口静默吞键（G7）。
    fn elevation(pid: u32) -> Option<bool> {
        // OpenProcessToken 需要 PROCESS_QUERY_INFORMATION；只用 LIMITED 会拿到句柄但查不了令牌
        let access = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION;
        let handle = match unsafe { OpenProcess(access, false, pid) } {
            Ok(h) => h,
            Err(_) => return None,
        };
        let result = token_is_elevated(handle);
        let _ = unsafe { CloseHandle(handle) };
        result
    }

    fn token_is_elevated(process: HANDLE) -> Option<bool> {
        let mut token = HANDLE::default();
        if unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) }.is_err() {
            return None;
        }
        let mut info = TOKEN_ELEVATION::default();
        let mut returned = 0u32;
        let ok = unsafe {
            GetTokenInformation(
                token,
                TokenElevation,
                Some(&mut info as *mut TOKEN_ELEVATION as *mut core::ffi::c_void),
                std::mem::size_of::<TOKEN_ELEVATION>() as u32,
                &mut returned,
            )
        }
        .is_ok();
        let _ = unsafe { CloseHandle(token) };
        if ok { Some(info.TokenIsElevated != 0) } else { None }
    }

    pub(crate) fn self_elevated() -> bool {
        elevation(unsafe { GetCurrentProcessId() }) == Some(true)
    }

    pub(crate) fn snapshot_focus() -> Option<FocusSnapshot> {
        let hwnd = unsafe { GetForegroundWindow() };
        if hwnd.0.is_null() {
            return None;
        }
        let mut pid = 0u32;
        unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid)) };
        let mut buf = [0u16; 256];
        let n = unsafe { GetClassNameW(hwnd, &mut buf) };
        let class_name = String::from_utf16_lossy(&buf[..n.max(0) as usize]);
        Some(FocusSnapshot {
            hwnd: hwnd.0 as isize,
            pid,
            class_name: class_name.clone(),
            elevated: elevation(pid),
            is_console: CONSOLE_CLASSES.contains(&class_name.as_str()),
        })
    }

    fn key_input(vk: u16, scan: u16, flags: KEYBD_EVENT_FLAGS) -> INPUT {
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VIRTUAL_KEY(vk),
                    wScan: scan,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: EXTRA_INFO_MAGIC,
                },
            },
        }
    }

    /// 发送一批事件，返回实际发送的 INPUT 数量（0 表示被拦）。
    fn send(events: &[Event]) -> usize {
        let mut inputs: Vec<INPUT> = Vec::with_capacity(events.len() * 2);
        for event in events {
            match *event {
                Event::Text(unit) => {
                    inputs.push(key_input(0, unit, KEYEVENTF_UNICODE));
                    inputs.push(key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP));
                }
                Event::Key(vk) => {
                    inputs.push(key_input(vk, 0, KEYBD_EVENT_FLAGS(0)));
                    inputs.push(key_input(vk, 0, KEYEVENTF_KEYUP));
                }
            }
        }
        if inputs.is_empty() {
            return 0;
        }
        unsafe { SendInput(&inputs, std::mem::size_of::<INPUT>() as i32) as usize }
    }

    /// ALT 技巧：直接 SetForegroundWindow 会随机失败（G6）。
    ///
    /// 顺序必须是 ALT 按下 → SetForegroundWindow → ALT 抬起。
    /// 先抬起 ALT 就等于没按，系统仍然拒绝把前台给后台进程。
    fn restore_focus(hwnd: isize) -> bool {
        let target = HWND(hwnd as *mut core::ffi::c_void);
        let alt_down = key_input(VK_MENU, 0, KEYBD_EVENT_FLAGS(0));
        let alt_up = key_input(VK_MENU, 0, KEYEVENTF_KEYUP);
        let size = std::mem::size_of::<INPUT>() as i32;
        unsafe { SendInput([alt_down].as_slice(), size) };
        let ok = unsafe { SetForegroundWindow(target) }.as_bool();
        unsafe { SendInput([alt_up].as_slice(), size) };
        ok
    }

    // ------------------------------------------------------ 焦点控件探测

    /// 前台窗口里正在输入的 Edit/RichEdit 子窗口；不是可读控件就返回 None。
    fn focused_edit() -> Option<HWND> {
        let mut gti = GUITHREADINFO::default();
        gti.cbSize = std::mem::size_of::<GUITHREADINFO>() as u32;
        if unsafe { GetGUIThreadInfo(0, &mut gti) }.is_err() {
            return None;
        }
        if gti.hwndFocus.0.is_null() {
            return None;
        }
        let mut buf = [0u16; 256];
        let n = unsafe { GetClassNameW(gti.hwndFocus, &mut buf) };
        let class_name = String::from_utf16_lossy(&buf[..n.max(0) as usize]);
        if !is_edit_class(&class_name) {
            return None;
        }
        Some(gti.hwndFocus)
    }

    /// SendMessageTimeoutW 包装：成功返回 lpdwresult，失败（返回 0/超时）返回 None。
    fn send_timeout(hwnd: HWND, msg: u32, wparam: usize, lparam: isize) -> Option<usize> {
        let mut out = 0usize;
        let ok = unsafe {
            SendMessageTimeoutW(
                hwnd,
                msg,
                WPARAM(wparam),
                LPARAM(lparam),
                SMTO_ABORTIFHUNG,
                50,
                Some(&mut out),
            )
        };
        if ok.0 != 0 { Some(out) } else { None }
    }

    /// 读焦点控件当前的文本快照；读不到返回 None（控制台/自绘/游戏）。
    pub(crate) fn probe_focus_text(limit: i32) -> Option<FocusText> {
        // Python 侧 limit 为负会让 create_unicode_buffer 抛异常 → except → None
        if limit < 0 {
            return None;
        }
        let hwnd = focused_edit()?;
        // WinForms 的 Edit 不认 EM_GETTEXTLENGTHEX，统一用 WM_GETTEXTLENGTH
        let length = send_timeout(hwnd, WM_GETTEXTLENGTH, 0, 0)?;
        let mut text = None;
        if length <= limit as usize {
            let mut buf = vec![0u16; limit as usize + 1];
            if send_timeout(hwnd, WM_GETTEXT, limit as usize + 1, buf.as_mut_ptr() as isize)
                .is_none()
            {
                return None;
            }
            let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
            text = Some(String::from_utf16_lossy(&buf[..end]));
        }
        Some(FocusText { length: length as i32, text })
    }

    // ------------------------------------------------------ 剪贴板

    /// 剪贴板读写。每个方法失败都返回 None/false，绝不 panic 打断输入。
    ///
    /// 单独成类是为了能替换成假实现做单测 —— 测试里绝不去碰用户真实剪贴板。
    pub(super) struct Clipboard;

    impl Clipboard {
        fn open(&self) -> bool {
            for _ in 0..OPEN_RETRIES.max(1) {
                if unsafe { OpenClipboard(HWND::default()) }.is_ok() {
                    return true;
                }
                std::thread::sleep(Duration::from_millis(OPEN_DELAY_MS));
            }
            false
        }

        fn close(&self) {
            let _ = unsafe { CloseClipboard() };
        }

        fn read(&self, fmt: u32) -> Option<Vec<u8>> {
            let handle = match unsafe { GetClipboardData(fmt) } {
                Ok(h) => h,
                Err(_) => return None,
            };
            let hg = HGLOBAL(handle.0);
            let size = unsafe { GlobalSize(hg) };
            if size == 0 {
                return None; // CF_BITMAP / CF_ENHMETAFILE 等句柄型格式，复制不了
            }
            let ptr = unsafe { GlobalLock(hg) };
            if ptr.is_null() {
                return None;
            }
            let data = unsafe { std::slice::from_raw_parts(ptr as *const u8, size) }.to_vec();
            let _ = unsafe { GlobalUnlock(hg) };
            Some(data)
        }

        fn set(&self, fmt: u32, data: &[u8]) -> bool {
            let handle = match unsafe { GlobalAlloc(GMEM_MOVEABLE, data.len()) } {
                Ok(h) => h,
                Err(_) => return false,
            };
            let ptr = unsafe { GlobalLock(handle) };
            if ptr.is_null() {
                let _ = unsafe { GlobalFree(handle) };
                return false;
            }
            unsafe { std::ptr::copy_nonoverlapping(data.as_ptr(), ptr as *mut u8, data.len()) };
            let _ = unsafe { GlobalUnlock(handle) };
            if unsafe { SetClipboardData(fmt, HANDLE(handle.0)) }.is_err() {
                let _ = unsafe { GlobalFree(handle) };
                return false;
            }
            true // 所有权已交给系统，不能再 Free
        }

        pub(super) fn snapshot(&self) -> Option<ClipboardSnapshot> {
            if !self.open() {
                return None;
            }
            let mut snap = ClipboardSnapshot::default();
            let mut fmt = 0u32;
            loop {
                fmt = unsafe { EnumClipboardFormats(fmt) };
                if fmt == 0 {
                    break;
                }
                match self.read(fmt) {
                    Some(data) => snap.formats.push((fmt, data)),
                    None => snap.skipped.push(fmt),
                }
            }
            self.close();
            Some(snap)
        }

        pub(super) fn text(&self) -> Option<String> {
            if unsafe { IsClipboardFormatAvailable(CF_UNICODETEXT) }.is_err() {
                return None;
            }
            if !self.open() {
                return None;
            }
            let result = (|| {
                let handle = match unsafe { GetClipboardData(CF_UNICODETEXT) } {
                    Ok(h) => h,
                    Err(_) => return None,
                };
                let hg = HGLOBAL(handle.0);
                let ptr = unsafe { GlobalLock(hg) };
                if ptr.is_null() {
                    return None;
                }
                // ctypes.wstring_at(ptr)：从指针处读 UTF-16，直到 NUL
                let mut units: Vec<u16> = Vec::new();
                let mut p = ptr as *const u16;
                unsafe {
                    while *p != 0 {
                        units.push(*p);
                        p = p.add(1);
                    }
                }
                let _ = unsafe { GlobalUnlock(hg) };
                Some(String::from_utf16_lossy(&units))
            })();
            self.close();
            result
        }

        pub(super) fn write(&self, text: &str) -> bool {
            if !self.open() {
                return false;
            }
            let result = (|| {
                if unsafe { EmptyClipboard() }.is_err() {
                    return false;
                }
                self.set(CF_UNICODETEXT, &utf16_bytes(text))
            })();
            self.close();
            result
        }

        pub(super) fn restore(&self, snap: &ClipboardSnapshot) -> bool {
            if !self.open() {
                return false;
            }
            let result = (|| {
                if unsafe { EmptyClipboard() }.is_err() {
                    return false;
                }
                let mut ok = true;
                for (fmt, data) in &snap.formats {
                    ok = self.set(*fmt, data) && ok;
                }
                ok
            })();
            self.close();
            result
        }
    }

    /// Python 侧的 `clipboard = Clipboard()` 模块级单例。
    static CLIPBOARD: Clipboard = Clipboard;

    /// utf-16-le 编码 + 双 NUL 结尾（SetClipboardData 的 CF_UNICODETEXT 格式要求）。
    fn utf16_bytes(text: &str) -> Vec<u8> {
        let mut bytes: Vec<u8> = text.encode_utf16().flat_map(u16::to_le_bytes).collect();
        bytes.extend_from_slice(&[0, 0]);
        bytes
    }

    fn send_unicode_text(text: &str, cfg: &InjectConfig) -> bool {
        let events = build_events(text);
        let mut total = 0usize;
        for batch in chunked(&events, cfg.chunk_units) {
            if send(&batch) == 0 {
                return false;
            }
            total += batch.len();
            if cfg.chunk_delay_ms != 0 {
                std::thread::sleep(Duration::from_millis(cfg.chunk_delay_ms));
            }
        }
        total == events.len()
    }

    /// Ctrl+V。事件带 MAGIC，PTT 钩子不会把它当成用户按了 Ctrl（G20）。
    fn send_paste() -> bool {
        let inputs = [
            key_input(VK_CONTROL, 0, KEYBD_EVENT_FLAGS(0)),
            key_input(VK_V, 0, KEYBD_EVENT_FLAGS(0)),
            key_input(VK_V, 0, KEYEVENTF_KEYUP),
            key_input(VK_CONTROL, 0, KEYEVENTF_KEYUP),
        ];
        let sent = unsafe { SendInput(&inputs, std::mem::size_of::<INPUT>() as i32) };
        sent as usize == inputs.len()
    }

    /// 粘贴后恢复快照；但若剪贴板已被别的程序改写，就不动它。
    fn restore_unless_replaced(snap: &ClipboardSnapshot, pasted: &str) -> bool {
        let current = CLIPBOARD.text();
        if current.as_deref() != Some(pasted) {
            eprintln!("[inject] 剪贴板已被其它程序改写，跳过恢复");
            return false;
        }
        if !CLIPBOARD.restore(snap) {
            eprintln!("[inject] ⚠ 剪贴板恢复失败（原内容可能已丢失）");
            return false;
        }
        true
    }

    /// 等 Ctrl+V 真正生效再恢复剪贴板。
    ///
    /// 读得到文本时轮询"内容是否变了"（最多 600ms）；读不到就按固定延时。
    /// 之后仍等满 `timeout_ms`（默认 500ms，G3），让目标应用把剪贴板读完 ——
    /// 恢复太早会把**原剪贴板内容**粘出来，比粘不出来更难排查。
    fn wait_paste_landed(before: Option<&FocusText>, timeout_ms: u64) -> Option<FocusText> {
        if before.is_none() {
            // 读不到目标文本时只能等固定时长；至少留 30ms 让目标读走剪贴板（G3）
            std::thread::sleep(Duration::from_millis(timeout_ms.max(30)));
            return None;
        }
        let start = Instant::now();
        let deadline = start + Duration::from_millis(timeout_ms.max(600));
        let mut now = probe_focus_text(TEXT_PROBE_LIMIT);
        while Instant::now() < deadline {
            if !definitely_unchanged(before, now.as_ref()) {
                break;
            }
            std::thread::sleep(Duration::from_millis(30));
            now = probe_focus_text(TEXT_PROBE_LIMIT);
        }
        let remaining_ms = timeout_ms as i64 - start.elapsed().as_millis() as i64;
        if remaining_ms > 0 {
            std::thread::sleep(Duration::from_millis(remaining_ms as u64));
        }
        now
    }

    /// Unicode 发完后等一下再判定"没变化"。
    ///
    /// `SendInput` 只是把 `WM_CHAR` 投进目标线程队列，我们立刻用 `SendMessage` 去读时
    /// 目标可能还没处理完 —— 实测会读到只打了两个字。只有连续观察到"没变化"才敢兜底，
    /// 否则会把同一句话打两遍。成功路径上第一次探测就变了，不付这个延时。
    fn wait_for_change(before: Option<&FocusText>, timeout_ms: u64) -> Option<FocusText> {
        let deadline = Instant::now() + Duration::from_millis(timeout_ms);
        let mut now = probe_focus_text(TEXT_PROBE_LIMIT);
        while definitely_unchanged(before, now.as_ref()) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(30));
            now = probe_focus_text(TEXT_PROBE_LIMIT);
        }
        now
    }

    fn paste_via_clipboard(
        text: &str,
        cfg: &InjectConfig,
        before: Option<&FocusText>,
    ) -> InjectResult {
        let failed = || InjectResult { outcome: InjectOutcome::Failed, strategy: "clipboard" };
        let Some(snap) = CLIPBOARD.snapshot() else {
            eprintln!("[inject] 剪贴板被占用，无法兜底");
            return failed();
        };
        if !snap.skipped.is_empty() {
            eprintln!(
                "[inject] 剪贴板有 {} 个格式无法快照（位图等），恢复时可能丢失",
                snap.skipped.len()
            );
        }
        if !CLIPBOARD.write(text) {
            return failed();
        }

        // G3：`SetClipboardData` 之后必须留 ≥30ms 再发 Ctrl+V，
        // 否则慢应用可能读到**上一次**的剪贴板内容
        std::thread::sleep(Duration::from_millis(30));
        let sent = send_paste();
        let landed = wait_paste_landed(before, cfg.clipboard_restore_ms);
        restore_unless_replaced(&snap, text);

        if !sent {
            return failed();
        }
        if definitely_unchanged(before, landed.as_ref()) {
            // 可探测且确实没粘进去：不能报成功（G13 同类，禁止"看起来成功了"）
            eprintln!("[inject] Ctrl+V 已发送但焦点控件文本未变化");
            return failed();
        }
        InjectResult { outcome: InjectOutcome::Injected, strategy: "clipboard" }
    }

    // ------------------------------------------------------ 对外接口

    fn focus_ok(current: &FocusSnapshot, snap: Option<&FocusSnapshot>, cfg: &InjectConfig) -> bool {
        if !cfg.verify_focus || snap.is_none() {
            return true;
        }
        let snap = snap.expect("checked above");
        if current.hwnd == snap.hwnd {
            return true;
        }
        cfg.restore_focus && restore_focus(snap.hwnd)
    }

    pub(super) fn inject_ex(
        text: &str,
        snap: Option<&FocusSnapshot>,
        cfg: &InjectConfig,
    ) -> InjectResult {
        if text.is_empty() {
            return InjectResult { outcome: InjectOutcome::Injected, strategy: "none" };
        }

        let Some(current) = snapshot_focus() else {
            return InjectResult { outcome: InjectOutcome::NoTarget, strategy: "none" };
        };

        if !focus_ok(&current, snap, cfg) {
            return InjectResult { outcome: InjectOutcome::FocusLost, strategy: "none" };
        }

        // 提权目标：SendInput 会被 UIPI 静默吞掉（G7），必须显式失败。
        // elevated=None 表示读不到令牌（受保护进程，如 Edge/Chrome 的 PPL）；
        // 这种情况下**不能**武断拒绝，否则浏览器输入框直接不可用。
        // 处理方式：按普通窗口尝试注入，失败由 SendInput 返回值暴露（见 G13）。
        if current.elevated == Some(true) && !self_elevated() {
            return InjectResult { outcome: InjectOutcome::ElevatedTarget, strategy: "none" };
        }

        // Python 侧在此处对原始字符串判"未知策略 → 显式 Failed"；Rust 的
        // InjectStrategy 已在 parse 处把未知值归到 Auto（见模块文档有意差异）。
        match cfg.strategy {
            InjectStrategy::Clipboard => {
                paste_via_clipboard(text, cfg, probe_focus_text(TEXT_PROBE_LIMIT).as_ref())
            }
            InjectStrategy::Unicode | InjectStrategy::Auto => {
                let before = if cfg.strategy == InjectStrategy::Auto {
                    probe_focus_text(TEXT_PROBE_LIMIT)
                } else {
                    None
                };
                let ok = send_unicode_text(text, cfg);
                let result = InjectResult {
                    outcome: if ok { InjectOutcome::Injected } else { InjectOutcome::Failed },
                    strategy: "unicode",
                };
                if cfg.strategy == InjectStrategy::Unicode || !ok {
                    return result;
                }

                // auto：只有"探测得到、且内容确实没变"才兜底。探测不到就到此为止，
                // 否则会在控制台/游戏里把同一句话打两遍。
                let after = wait_for_change(before.as_ref(), WAIT_FOR_CHANGE_MS);
                if !definitely_unchanged(before.as_ref(), after.as_ref()) {
                    return result;
                }

                eprintln!("[inject] 焦点控件文本未变化 → 改走剪贴板兜底");
                paste_via_clipboard(text, cfg, before.as_ref())
            }
        }
    }

    pub(super) fn describe() -> String {
        let Some(s) = snapshot_focus() else {
            return "inject: 无可注入目标（前台窗口为空）".to_string();
        };
        let flag = match s.elevated {
            Some(true) => "提权",
            Some(false) => "普通",
            None => "未知/受保护",
        };
        let console = if s.is_console { "，控制台类" } else { "" };
        format!(
            "inject: 目标 hwnd={} pid={} class={}（{}{}）",
            s.hwnd, s.pid, s.class_name, flag, console
        )
    }
}

// ---------------------------------------------------------------- 对外包装
//
// 非 Windows 平台的 stub：与 Python 的 `IS_WINDOWS` 分支语义一致
// （inject_ex → Failed/"none"，探测/快照 → None，self_elevated → False）。

/// 与 Python `TEXT_PROBE_LIMIT` 同值（对照读代码用；实际上限在 win32 模块内）。
#[allow(dead_code)]
pub const TEXT_PROBE_LIMIT: i32 = 4096;

// snapshot_focus / probe_focus_text 的 re-export 区。
// win32 版本：snapshot_focus 已在 voice.rs 直接使用；probe_focus_text 也一样。
#[cfg(windows)]
pub(crate) use win32::snapshot_focus;

#[cfg(not(windows))]
pub fn snapshot_focus() -> Option<FocusSnapshot> {
    None
}

#[cfg(not(windows))]
pub fn probe_focus_text(_limit: i32) -> Option<FocusText> {
    None
}

#[cfg(not(windows))]
pub fn self_elevated() -> bool {
    false
}

/// 把文本打到当前光标处，并告诉调用方实际用了哪种策略。不 panic。
#[cfg(windows)]
pub fn inject_ex(text: &str, snap: Option<&FocusSnapshot>, cfg: &InjectConfig) -> InjectResult {
    win32::inject_ex(text, snap, cfg)
}

#[cfg(not(windows))]
pub fn inject_ex(_text: &str, _snap: Option<&FocusSnapshot>, _cfg: &InjectConfig) -> InjectResult {
    InjectResult { outcome: InjectOutcome::Failed, strategy: "none" }
}

/// 兼容入口：只要结果，不要策略。（P2 接 Trace/纠错链路时使用）
#[allow(dead_code)]
pub fn inject(text: &str, snap: Option<&FocusSnapshot>, cfg: &InjectConfig) -> InjectOutcome {
    inject_ex(text, snap, cfg).outcome
}

/// 人话描述当前注入目标（诊断用，`voice_test describe` 接线时使用）。
#[cfg(windows)]
#[allow(dead_code)]
pub fn describe() -> String {
    win32::describe()
}

#[cfg(not(windows))]
#[allow(dead_code)]
pub fn describe() -> String {
    "inject: 非 Windows，注入不可用（返回 Failed）".to_string()
}

// ---------------------------------------------------------------- 单测

#[cfg(test)]
mod tests {
    use super::*;

    /// 'VIME' 魔数：与 hotkey.py / hotkey.rs 同步，别改
    #[test]
    fn inject_magic_is_vime() {
        assert_eq!(EXTRA_INFO_MAGIC, 0x5649_4D45);
    }

    /// 换行必须变 VK_RETURN、制表必须变 VK_TAB，其余按 UTF-16 码元原样发
    #[test]
    fn inject_build_events_maps_special_chars() {
        assert_eq!(
            build_events("a\nb\tc"),
            vec![
                Event::Text(b'a' as u16),
                Event::Key(VK_RETURN),
                Event::Text(b'b' as u16),
                Event::Key(VK_TAB),
                Event::Text(b'c' as u16),
            ]
        );
    }

    /// \r\n 与孤立的 \r 都归一成单个换行，不能敲出两个回车
    #[test]
    fn inject_build_events_normalizes_newlines() {
        assert_eq!(
            build_events("x\r\ny\rz"),
            vec![
                Event::Text(b'x' as u16),
                Event::Key(VK_RETURN),
                Event::Text(b'y' as u16),
                Event::Key(VK_RETURN),
                Event::Text(b'z' as u16),
            ]
        );
        // "\r\r\n"：先替换 \r\n，再替换孤立的 \r → 两个换行
        assert_eq!(
            build_events("\r\r\n"),
            vec![Event::Key(VK_RETURN), Event::Key(VK_RETURN)]
        );
    }

    /// emoji 按 UTF-16 码元发：😀 = 2 码元，按字符拆会乱码（G2）
    #[test]
    fn inject_build_events_uses_utf16_units_for_emoji() {
        assert_eq!(build_events("😀"), vec![Event::Text(0xD83D), Event::Text(0xDE00)]);
    }

    #[test]
    fn inject_build_events_empty_text() {
        assert!(build_events("").is_empty());
    }

    /// 每批 size 个码元；size=0 时整段作为一批（Python `size <= 0` 分支）
    #[test]
    fn inject_chunked_splits_by_size() {
        let events = build_events("abcdef");
        let chunks = chunked(&events, 2);
        assert_eq!(chunks.len(), 3);
        assert_eq!(chunks[0], vec![Event::Text(b'a' as u16), Event::Text(b'b' as u16)]);
        assert_eq!(chunks[2], vec![Event::Text(b'e' as u16), Event::Text(b'f' as u16)]);
        assert_eq!(chunked(&events, 10).len(), 1);
        assert_eq!(chunked(&events, 0).len(), 1);
        assert!(chunked(&[], 0).is_empty());
        assert!(chunked(&[], 4).is_empty());
    }

    /// 任一端读不到 → False：探测不到不能兜底，否则同一句话打两遍
    #[test]
    fn inject_unchanged_requires_both_probes() {
        assert!(!definitely_unchanged(None, None));
        let ft = FocusText { length: 5, text: Some("hello".to_string()) };
        assert!(!definitely_unchanged(None, Some(&ft)));
        assert!(!definitely_unchanged(Some(&ft), None));
    }

    /// 能读到全文时比全文（粘贴替换等长选区也能识别出来）
    #[test]
    fn inject_unchanged_compares_text_when_available() {
        let a = FocusText { length: 3, text: Some("abc".to_string()) };
        let b = FocusText { length: 3, text: Some("abc".to_string()) };
        let c = FocusText { length: 3, text: Some("xyz".to_string()) };
        assert!(definitely_unchanged(Some(&a), Some(&b)));
        assert!(!definitely_unchanged(Some(&a), Some(&c)));
    }

    /// 太长只读了长度时退回比长度
    #[test]
    fn inject_unchanged_falls_back_to_length() {
        let full = FocusText { length: 100, text: Some("abc".to_string()) };
        let long = FocusText { length: 100, text: None };
        let long2 = FocusText { length: 100, text: None };
        let other = FocusText { length: 101, text: None };
        assert!(definitely_unchanged(Some(&full), Some(&long)));
        assert!(definitely_unchanged(Some(&long), Some(&long2)));
        assert!(!definitely_unchanged(Some(&full), Some(&other)));
    }

    /// WinForms 的 Edit 类名带后缀，必须前缀匹配（不能只做等值）
    #[test]
    fn inject_is_edit_class_matches_forms_suffix() {
        for name in ["Edit", "RICHEDIT", "RichEdit20W", "RichEdit50W", "Scintilla"] {
            assert!(is_edit_class(name), "{name}");
        }
        assert!(is_edit_class("WindowsForms10.EDIT.app.0.xxx"));
        assert!(is_edit_class("WindowsForms10.RichEdit20W.app.0.xxx"));
        for name in ["Button", "Chrome_WidgetWin_1", "WindowsForms10.BUTTON.app.0.x"] {
            assert!(!is_edit_class(name), "{name}");
        }
    }

    /// 大小写不敏感 + 去空白；空值按 unicode（Python：`cfg.strategy or "unicode"`）；
    /// 未知值按 Auto（三态枚举装不下"非法"，见模块文档有意差异）
    #[test]
    fn inject_strategy_parse_normalizes() {
        assert_eq!(InjectStrategy::parse("unicode"), InjectStrategy::Unicode);
        assert_eq!(InjectStrategy::parse("CLIPBOARD"), InjectStrategy::Clipboard);
        assert_eq!(InjectStrategy::parse(" auto "), InjectStrategy::Auto);
        assert_eq!(InjectStrategy::parse(""), InjectStrategy::Unicode);
        assert_eq!(InjectStrategy::parse("nonsense"), InjectStrategy::Auto);
        assert_eq!(InjectStrategy::default(), InjectStrategy::Auto);
    }

    /// as_str 的值与 Python Enum 的值一致（写 Trace 用）
    #[test]
    fn inject_outcome_as_str_values() {
        assert_eq!(InjectOutcome::Injected.as_str(), "Injected");
        assert_eq!(InjectOutcome::FocusLost.as_str(), "FocusLost");
        assert_eq!(InjectOutcome::ElevatedTarget.as_str(), "ElevatedTarget");
        assert_eq!(InjectOutcome::Failed.as_str(), "Failed");
        assert_eq!(InjectOutcome::NoTarget.as_str(), "NoTarget");
    }

    /// 默认配置与 inject.py 的 dataclass 默认值一致
    #[test]
    fn inject_config_defaults_match_python() {
        let cfg = InjectConfig::default();
        assert_eq!(cfg.strategy, InjectStrategy::Auto);
        assert!(!cfg.restore_focus);
        assert_eq!(cfg.chunk_units, CHUNK_UNITS);
        assert_eq!(cfg.chunk_delay_ms, CHUNK_DELAY_MS);
        assert!(cfg.verify_focus);
        assert_eq!(cfg.clipboard_restore_ms, 500); // G3
    }
}
