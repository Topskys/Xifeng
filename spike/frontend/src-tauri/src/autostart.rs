//! 开机自启（D16 P1d）：写当前用户 `Run` 键，指向**壳 exe**。
//!
//! 与 Python `autostart.py` 的关系：值名同为 `Xifeng`（HKCU\...\Run），
//! 所以状态互通——Python bootstrap 返回的 `autostart` 布尔值读同一个键，
//! 前端初始状态无需改动。差异只在命令行：Python 版指向 sidecar
//! `--tray`（语音服务进程，D16 已退役），这里指向壳 exe（托盘常驻，
//! 窗口启动即显示——托盘在后台守热键，用户点图标即可）。
//!
//! 为什么迁到壳：D16 后语音触发链在壳里，自启目标必须是壳。

use std::path::PathBuf;

use serde_json::{json, Value};

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
const VALUE_NAME: &str = "Xifeng";
/// 改名（D21）：旧版写的是 `VoiceIME`，迁移后删掉，避免开机跑两次。
const LEGACY_VALUE_NAME: &str = "VoiceIME";

/// 自启命令：壳 exe 绝对路径（含空格自动带引号）。
fn shell_command() -> String {
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("xifeng.exe"));
    format!("\"{}\"", exe.display())
}

/// 是否已开启（读键存在即认为开启，与 Python `is_enabled` 同语义）。
#[cfg(windows)]
pub fn is_enabled() -> bool {
    winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
        .open_subkey(RUN_KEY)
        .and_then(|k| k.get_value::<String, _>(VALUE_NAME))
        .is_ok()
}

#[cfg(windows)]
pub fn set(enabled: bool) -> Result<(), String> {
    use winreg::enums::{KEY_QUERY_VALUE, KEY_SET_VALUE, HKEY_CURRENT_USER};
    use winreg::RegKey;

    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    if enabled {
        let key = hkcu
            .open_subkey_with_flags(RUN_KEY, KEY_SET_VALUE)
            .map_err(|e| format!("打开 Run 键失败：{e}"))?;
        key.set_value(VALUE_NAME, &shell_command())
            .map_err(|e| format!("写入自启失败：{e}"))
    } else {
        match hkcu.open_subkey_with_flags(RUN_KEY, KEY_QUERY_VALUE | KEY_SET_VALUE) {
            Ok(key) => {
                // 新旧值都清：关掉之后不应该还有任何一项在开机时拉起旧版
                for name in [VALUE_NAME, LEGACY_VALUE_NAME] {
                    match key.delete_value(name) {
                        Ok(()) => {}
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                        Err(e) => return Err(format!("删除自启失败：{e}")),
                    }
                }
                Ok(())
            }
            // 键不存在 = 本来就没开
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(format!("打开 Run 键失败：{e}")),
        }
    }
}

/// 旧版自启值迁移（D21）：把 `VoiceIME` 值名迁到 `Xifeng`。
///
/// 这一条同时覆盖两代历史值：Python 时代的 `...\VoiceIME-Core --tray`
/// 和上一版壳的 `...\voiceime-tauri.exe`。两者都只代表"用户想开机自启"，
/// 升级后目标一律改成本壳 exe（D16 后触发链在壳里），旧值名删除。
///
/// 返回被替换的旧命令供日志；`Ok(None)` = 没开自启（旧值名不存在）。
#[cfg(windows)]
pub fn migrate_legacy() -> Result<Option<String>, String> {
    use winreg::enums::{KEY_QUERY_VALUE, KEY_SET_VALUE, HKEY_CURRENT_USER};
    use winreg::RegKey;

    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let key = hkcu
        .open_subkey_with_flags(RUN_KEY, KEY_QUERY_VALUE | KEY_SET_VALUE)
        .map_err(|e| format!("打开 Run 键失败：{e}"))?;
    let old: String = match key.get_value(LEGACY_VALUE_NAME) {
        Ok(v) => v,
        // 没开自启：迁移无事可做（不算错误）
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("读自启值失败：{e}")),
    };
    key.set_value(VALUE_NAME, &shell_command())
        .map_err(|e| format!("迁移自启值失败：{e}"))?;
    match key.delete_value(LEGACY_VALUE_NAME) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("删除旧自启值失败：{e}")),
    }
    Ok(Some(old))
}

#[cfg(not(windows))]
pub fn migrate_legacy() -> Result<Option<String>, String> {
    Ok(None)
}

/// Tauri 命令：`autostart_cmd({enabled?: bool})`。
/// 带 `enabled` 时设置（返回 `{ok, error}`，与 Python `autostart` 方法同形）；
/// 不带时返回当前状态。
#[tauri::command]
pub fn autostart_cmd(enabled: Option<bool>) -> Result<Value, String> {
    #[cfg(windows)]
    {
        match enabled {
            // 与 Python autostart 方法同形：{ok, error}，错误不抛给前端弹窗
            Some(v) => Ok(match set(v) {
                Ok(()) => json!({ "ok": true, "error": "" }),
                Err(e) => json!({ "ok": false, "error": e }),
            }),
            None => Ok(json!({ "ok": true, "enabled": is_enabled() })),
        }
    }
    #[cfg(not(windows))]
    {
        let _ = enabled;
        Ok(json!({ "ok": false, "error": "非 Windows 平台不支持自启" }))
    }
}
