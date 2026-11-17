//! 托盘常驻（D16 P1d）：两项菜单——显示主界面 / 退出。
//!
//! 页面切换、开机自启都在主窗口里做（2026-11-13 用户决策：托盘只要两项）。
//! 主窗口点 × 不退出，隐藏到托盘（输入法类应用的标准行为：后台要守着热键），
//! 真正退出走托盘「退出」或托盘图标右键。

use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};

/// 安装托盘。失败返回 Err 由调用方记日志（托盘缺失不致命，语音照常工作）。
pub fn install(app: &AppHandle) -> Result<(), String> {
    let show = MenuItemBuilder::with_id("show", "显示主界面")
        .build(app)
        .map_err(|e| e.to_string())?;
    let quit = MenuItemBuilder::with_id("quit", "退出")
        .build(app)
        .map_err(|e| e.to_string())?;
    let separator = PredefinedMenuItem::separator(app).map_err(|e| e.to_string())?;

    let menu = MenuBuilder::new(app)
        .item(&show)
        .item(&separator)
        .item(&quit)
        .build()
        .map_err(|e| e.to_string())?;

    let icon = app
        .default_window_icon()
        .cloned()
        .ok_or("缺少窗口图标（bundle 配置）")?;

    let tray = TrayIconBuilder::with_id("xifeng")
        .icon(icon)
        .tooltip("Xifeng · 按住 Ctrl 说话")
        .menu(&menu)
        .show_menu_on_left_click(false) // 左键单击 = 显示主界面（同 Python 版 default 行为）
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main(app),
            "quit" => {
                // RunEvent::Exit 已挂 bridge.shutdown()，先礼后兵收 Python 核
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            // 左键单击（抬起）= 显示主界面；右键弹菜单
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        })
        .build(app)
        .map_err(|e| e.to_string())?;

    // TrayIcon 被 drop 就消失：交给 Tauri state 管（进程生命周期内常驻）
    app.manage(tray);
    Ok(())
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}
