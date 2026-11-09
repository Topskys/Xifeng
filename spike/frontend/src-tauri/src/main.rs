//! VoiceIME 桌面壳：Tauri 窗口 + Python 核心子进程。
//!
//! 职责边界：
//! - 壳只做「窗口 + 进程 + 转发」，业务逻辑全在 Python 核里；
//! - 前端只认一个 command：`py_call(method, params)`，参数与方法表见
//!   `docs/03-core-concepts/ipc-contract.md`，与 HTTP 路由一一对应。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod py;

use std::sync::Arc;

use serde_json::{Map, Value};
use tauri::{RunEvent, State};

use py::PythonBridge;

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

fn main() {
    let bridge = Arc::new(PythonBridge::new());

    let app = tauri::Builder::default()
        .manage(Arc::clone(&bridge))
        .invoke_handler(tauri::generate_handler![py_call])
        .setup({
            let bridge = Arc::clone(&bridge);
            move |app| {
                // 同步等 ready：前端第一步就是 bootstrap，核没起来它也没法干活。
                // 失败不 panic——窗口照样开，前端会从 py_call 的错误里看到原因。
                if let Err(e) = bridge.start(app.handle().clone()) {
                    eprintln!("[shell] Python 核启动失败：{e}");
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
