/* 端点 ↔ IPC 方法对照表：与 `core/xifeng/webgui.py` 的 Api 方法一一对应。
 *
 * 加新端点时三处同步：这张表、`webgui.Api` 的方法、`ipc.py` 的 METHODS。
 */

export type ApiMethod =
  | "bootstrap"
  | "stats"
  | "history"
  | "history_delete"
  | "settings_save"
  | "settings_reset"
  | "theme"
  | "models_use"
  | "models_find"
  | "models_download_start"
  | "models_download_status"
  | "models_download_cancel"
  | "autostart"
  | "prune"
  | "open"
  | "check_update"
  | "asr_stream_cancel";

/** HTTP path ↔ IPC method 对照表，与 webgui.py 的 Api 方法一一对应。 */
export const ROUTES: Record<string, { method: ApiMethod; verb: "GET" | "POST" }> = {
  "/api/bootstrap": { method: "bootstrap", verb: "GET" },
  "/api/stats": { method: "stats", verb: "GET" },
  "/api/history": { method: "history", verb: "GET" },
  "/api/history/delete": { method: "history_delete", verb: "POST" },
  "/api/settings/save": { method: "settings_save", verb: "POST" },
  "/api/settings/reset": { method: "settings_reset", verb: "POST" },
  "/api/theme": { method: "theme", verb: "POST" },
  "/api/models/use": { method: "models_use", verb: "POST" },
  "/api/models/find": { method: "models_find", verb: "POST" },
  "/api/models/download/start": { method: "models_download_start", verb: "POST" },
  "/api/models/download/status": { method: "models_download_status", verb: "POST" },
  "/api/models/download/cancel": { method: "models_download_cancel", verb: "POST" },
  "/api/autostart": { method: "autostart", verb: "POST" },
  "/api/prune": { method: "prune", verb: "POST" },
  "/api/open": { method: "open", verb: "POST" },
  "/api/check_update": { method: "check_update", verb: "POST" },
  "/api/stream/cancel": { method: "asr_stream_cancel", verb: "POST" },
}