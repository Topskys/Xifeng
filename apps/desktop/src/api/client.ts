/* api 客户端：把端点调用分发到 Tauri IPC 或本地 HTTP。
 *
 * 页面只认 `api.xxx()`，不关心底下走哪条通道。
 */

import { ROUTES } from "@/api/routes"
import { httpCall, tauriInvoke, unwrapIpc } from "@/api/transport"
import type {
  Bootstrap,
  HistoryRow,
  ModelInfo,
  Settings,
  StatsPayload,
} from "@/api/types"

async function call<T>(path: string, params: Record<string, unknown> = {}): Promise<T> {
  const route = ROUTES[path]
  if (!route) throw new Error(`未登记的端点：${path}`)
  const invoke = tauriInvoke
  if (invoke) {
    // D16：自启归壳管（注册表目标必须是壳 exe），Tauri 环境走 Rust 命令；
    // 浏览器（HTTP 开发模式）仍走 Python autostart 方法
    if (path === "/api/autostart") {
      return unwrapIpc<T>(await invoke("autostart_cmd", params))
    }
    // P2-① 设置热更：保存成功后让壳重新读 settings（下一次语音会话生效）。
    // 旧壳没有这条命令时忽略——改设置仍可用，重启后生效。
    if (route.method === "settings_save" || route.method === "settings_reset") {
      const res = await invoke("py_call", { method: route.method, params })
      try {
        await invoke("voice_reload_settings")
      } catch {
        /* 旧壳：无热更命令，保持重启生效 */
      }
      return unwrapIpc<T>(res)
    }
    return unwrapIpc<T>(await invoke("py_call", { method: route.method, params }))
  }
  return httpCall<T>(path, route.verb, params)
}

export const api = {
  bootstrap: () => call<Bootstrap>("/api/bootstrap"),
  stats: (period: string) => call<StatsPayload>("/api/stats", { period }),
  history: (period: string, offset: number, limit: number) =>
    call<{ rows: HistoryRow[]; has_more: boolean }>("/api/history", { period, offset, limit }),
  deleteHistory: (id: number) => call<{ ok: boolean }>("/api/history/delete", { id }),
  saveSettings: (patch: Partial<Settings>) => call<Settings>("/api/settings/save", patch),
  resetSettings: () => call<Settings>("/api/settings/reset"),
  setTheme: (theme: string) => call<Settings>("/api/theme", { theme }),
  modelsFind: (name: string, files: string[]) =>
    call<{ matches: string[] }>("/api/models/find", { name, files }),
  useModel: (path: string) =>
    call<{ current: string; list: ModelInfo[] }>("/api/models/use", { path }),
  toggleAutostart: (enabled: boolean) =>
    call<{ ok: boolean; error: string }>("/api/autostart", { enabled }),
  prune: () => call<{ removed: number }>("/api/prune"),
  open: (path: string) => call<{ ok: boolean }>("/api/open", { path }),
  checkUpdate: () =>
    call<{ latest: string; current: string; update: boolean } | { error: string }>(
      "/api/check_update",
    ),
}
