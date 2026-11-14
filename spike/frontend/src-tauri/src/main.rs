//! VoiceIME 桌面壳：Tauri 窗口 + 热键/采集/注入（D16）+ Python 模型运行时。
//!
//! 职责边界（D16 迁移后）：
//! - 壳（Rust）：热键钩子、cpal 采集、ime-post 后处理、SendInput 注入、
//!   窗口与托盘（P1d）；语音编排见 `voice.rs`；
//! - Python 核：ASR/纠错推理，经 IPC（`py_call` / `asr_begin|feed|end|cancel`），
//!   方法表见 `docs/03-core-concepts/ipc-contract.md`。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod autostart;
mod hotkey;
mod inject;
mod py;
mod tray;
mod voice;

use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};
use tauri::{Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder};

use py::PythonBridge;

/// 设置热更（P2-①）：前端保存设置后调用，重新从核心读 settings。
/// 改动在**下一次语音会话开始**生效（会话中段换配置没有意义）。
#[tauri::command]
async fn voice_reload_settings(
    bridge: State<'_, Arc<PythonBridge>>,
    cfg: State<'_, Arc<Mutex<voice::VoiceSettings>>>,
) -> Result<(), String> {
    let bridge = Arc::clone(bridge.inner());
    let cfg = Arc::clone(cfg.inner());
    tauri::async_runtime::spawn_blocking(move || {
        *cfg.lock().unwrap() = voice::VoiceSettings::load(&bridge);
        eprintln!("[shell] 设置已热更（下一次语音会话生效）");
    })
    .await
    .map_err(|e| format!("设置热更异常：{e}"))?;
    Ok(())
}

/// 前端唯一入口：把一次方法调用透传给 Python 核。
///
/// 阻塞在 `spawn_blocking` 里等 Python 应答，避免卡住 Tauri 的 async 运行时
/// （一条慢请求不该冻住窗口）。错误直接以字符串回给前端做横幅。
#[tauri::command]
async fn py_call(
    bridge: State<'_, Arc<PythonBridge>>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let bridge = Arc::clone(bridge.inner());
    // 契约里 params 恒为对象；前端不传时补 `{}`，省得 Python 侧再判 null。
    let params = params
        .filter(|v| !v.is_null())
        .unwrap_or_else(|| Value::Object(Map::new()));

    tauri::async_runtime::spawn_blocking(move || bridge.call(&method, params))
        .await
        .map_err(|e| format!("py_call 任务异常中止：{e}"))?
}

/// 无边框窗口的拖动：由前端标题栏 mousedown 触发。
///
/// 为什么不用 `data-tauri-drag-region`：它靠 Tauri 注入的脚本钩子，而本项目
/// 走自定义协议 + capabilities，实测在非按钮的空白处不总生效。显式命令可控。
#[tauri::command]
fn start_dragging(window: tauri::Window) -> Result<(), String> {
    window.start_dragging().map_err(|e| e.to_string())
}

#[tauri::command]
fn minimize(window: tauri::Window) -> Result<(), String> {
    window.minimize().map_err(|e| e.to_string())
}

#[tauri::command]
fn toggle_maximize(window: tauri::Window) -> Result<bool, String> {
    let maxed = window.is_maximized().map_err(|e| e.to_string())?;
    if maxed {
        window.unmaximize().map_err(|e| e.to_string())?;
    } else {
        window.maximize().map_err(|e| e.to_string())?;
    }
    Ok(!maxed)
}

#[tauri::command]
fn is_maximized(window: tauri::Window) -> Result<bool, String> {
    window.is_maximized().map_err(|e| e.to_string())
}

#[tauri::command]
fn close(window: tauri::Window) -> Result<(), String> {
    window.close().map_err(|e| e.to_string())
}

/// 合成按键驱动 e2e（仅沙箱验证用）：给真实钩子喂 Ctrl 按下/松开。
///
/// - `tap`：单击一次（按下→150ms→松开，作为双击的第一击）
/// - `down`/`up`：按住/松开（双击的第二击长按用 CDP 时序自己控制）
/// 事件走真实 WH_KEYBOARD_LL 链路（SendInput 不带 MAGIC），验得完整。
#[tauri::command]
async fn voice_test(mode: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || match mode.as_str() {
        "tap" => {
            voice::send_ctrl(true);
            std::thread::sleep(std::time::Duration::from_millis(150));
            voice::send_ctrl(false);
            Ok(())
        }
        "down" => {
            voice::send_ctrl(true);
            Ok(())
        }
        "up" => {
            voice::send_ctrl(false);
            Ok(())
        }
        // 注入 smoke：把固定文本打到当前焦点（e2e 里焦点=壳自己的 webview）。
        // 同时验证 G20：注入事件带 MAGIC，自家钩子不把它当用户按键。
        "inject" => {
            let res = voice::test_inject("测试注入 e2e");
            eprintln!(
                "[voice] inject smoke：outcome={} strategy={}",
                res.outcome.as_str(),
                res.strategy
            );
            Ok(())
        }
        m => Err(format!("未知 voice_test 模式：{m}（tap|down|up|inject）")),
    })
    .await
    .map_err(|e| format!("voice_test 异常：{e}"))?
}

/// 录音浮窗：屏幕底部居中的小胶囊（360×60 逻辑像素），前端按
/// `?window=overlay` 渲染精简页（音量条），录音会话中由编排器 show/hide。
fn build_overlay(app: &tauri::AppHandle) -> Result<(), String> {
    // 位置：主显示器底部居中（Win10/11 任务栏上方）
    let (x, y) = match app.primary_monitor().ok().flatten() {
        Some(m) => {
            let scale = m.scale_factor();
            let w = m.size().width as f64 / scale;
            let h = m.size().height as f64 / scale;
            ((w - 360.0) / 2.0, h - 130.0)
        }
        None => (200.0, 600.0),
    };

    WebviewWindowBuilder::new(
        app,
        "overlay",
        WebviewUrl::App("index.html?window=overlay".into()),
    )
    .title("VoiceIME 录音")
    .decorations(false)
    .transparent(true)
    .always_on_top(true)
    .skip_taskbar(true)
    .resizable(false)
    .focused(false)
    .visible(false)
    .inner_size(360.0, 60.0)
    .position(x, y)
    .build()
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn main() {
    // WebView2 数据目录 = exe 同级 EBWebView（便携：数据跟程序走）。
    // MUST 显式设置：默认目录（%LOCALAPPDATA%\com.voiceime.app）在本机被
    // 系统 webview 实例抢锁，创建报 0x800700AA（资源占用）；实测
    // AppData\Local 全部路径均失败、exe 同级成功（实验 A-D，2026-11-14）。
    let ud = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("EBWebView")))
        .unwrap_or_else(|| std::path::PathBuf::from("EBWebView"));
    if let Some(parent) = ud.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &ud);

    let bridge = Arc::new(PythonBridge::new());
    let hook = hotkey::Hook::start();

    let app = tauri::Builder::default()
        .manage(Arc::clone(&bridge))
        .manage(Arc::clone(&hook))
        .invoke_handler(tauri::generate_handler![
            py_call,
            voice_test,
            voice_reload_settings,
            autostart::autostart_cmd,
            start_dragging,
            minimize,
            toggle_maximize,
            is_maximized,
            close
        ])
        // 主窗口 × 不退出：隐藏到托盘（语音触发链要常驻后台）
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    let _ = window.hide();
                    api.prevent_close();
                }
            }
        })
        .setup({
            let bridge = Arc::clone(&bridge);
            let hook = Arc::clone(&hook);
            move |app| {
                // 同步等 ready：前端第一步就是 bootstrap，核没起来它也没法干活。
                // 失败不 panic——窗口照样开，前端会从 py_call 的错误里看到原因。
                if let Err(e) = bridge.start(app.handle().clone()) {
                    eprintln!("[shell] Python 核启动失败：{e}");
                }
                // 录音浮窗（D16：webview 浮窗，Tk 截图模糊方案退役）：
                // 无边框、透明、置顶、不进任务栏，初始隐藏，录音会话中 show/hide。
                // 延后创建：setup 阶段连建两个 WebView2 会撞 0x800700AA（资源占用，
                // 环境未初始化完），等主窗口环境就绪后再建（Tauri 已知问题）。
                {
                    let app = app.handle().clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(std::time::Duration::from_millis(600));
                        let _ = app.run_on_main_thread({
                            let app = app.clone();
                            move || {
                                if let Err(e) = build_overlay(&app) {
                                    eprintln!("[shell] 浮窗创建失败：{e}");
                                }
                            }
                        });
                    });
                }
                // 托盘两项：显示主界面 / 退出（D16 P1d，用户决策）
                if let Err(e) = tray::install(app.handle()) {
                    eprintln!("[shell] 托盘安装失败：{e}");
                }
                // D16 P1b：热键采集与语音编排迁入壳。**不再拉起 Python
                // --ptt --tray 语音服务**——两个钩子会双重触发、双重注入。
                if hook.active() {
                    let app = app.handle().clone();
                    let hook = std::clone::Clone::clone(&hook); // 编排线程一份，else 分支还要用
                    // 设置共享格：voice::run 首次装载；voice_reload_settings 热更
                    let cfg = Arc::new(Mutex::new(voice::VoiceSettings::load(&bridge)));
                    app.manage(Arc::clone(&cfg));
                    let spawned = std::thread::Builder::new()
                        .name("voice-orchestrator".into())
                        .spawn(move || voice::run(bridge, hook, app, cfg));
                    if let Err(e) = spawned {
                        // 降级不 panic：窗口/托盘仍可用，只是语音不可用
                        eprintln!("[shell] 起语音编排线程失败：{e}（语音功能不可用）");
                    }
                } else {
                    eprintln!(
                        "[shell] 键盘钩子装不上（error={}），语音功能不可用；\
                         需管理员/安全软件排查（G21/G24）",
                        hook.error()
                    );
                }
                Ok(())
            }
        })
        .build(tauri::generate_context!())
        .expect("Tauri 初始化失败");

    app.run(move |_app, event| match event {
        // 先礼后兵：shutdown() 内部先发 shutdown 方法，超时才 kill + wait 收尸。
        // 两个事件都挂上，谁先到谁负责，函数自身可重入。
        RunEvent::ExitRequested { .. } | RunEvent::Exit => bridge.shutdown(),
        _ => {}
    });
}
