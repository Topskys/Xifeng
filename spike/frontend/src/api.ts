/* Python 桥：Tauri 环境走 IPC（py_call），浏览器环境走本地 HTTP（127.0.0.1），数据不出本机。
 *
 * 传输层在模块加载时定一次，api 的方法签名/返回值不变，页面组件零改动：
 *   - 有 window.__TAURI__（或 window.__TAURI_INTERNALS__）→ core.invoke("py_call", { method, params })
 *   - 否则 → fetch(path)，与旧实现完全一致
 * 映射关系见 docs/03-core-concepts/ipc-contract.md 的方法表。
 */

export interface Settings {
  inject_strategy: string
  restore_focus: boolean
  clipboard_restore_ms: number
  filler_level: string
  final_punct: string
  space_between_cjk_latin: boolean
  trim_silence: boolean
  use_vad: boolean
  hotkey: string
  model_dir: string
  threads: number
  download_source: string
  theme: string
  show_popup: boolean
  model_correct: boolean
  popup_color: string
}

export interface ModelInfo {
  path: string
  name: string
  quant: string
  size: number
  ready: boolean
  current: boolean
}

export interface HistoryRow {
  id: number
  ts: string
  mode: string
  outcome: string
  text: string | null
  raw_text: string | null
  clean_text: string | null
  chars: number
  speech_ms: number
  e2e_ms: number
  ms_asr: number
  ms_post: number
  ms_inject: number
  strategy: string
  error: string
  fixes: number
}

export interface Kpis {
  p50: number
  p95: number
  charsToday: number
  charsAll: number
  total: number
  injected: number
  successRate: number
  failed: number
  dropped: number
  timeouts: number
}

export interface StageRow {
  ts: string
  ms_asr: number
  ms_post: number
  ms_inject: number
  e2e_ms: number
}

export interface DailyPoint {
  date: string
  count: number
  chars: number
  avg_e2e: number
}

export interface StatsPayload {
  threads: number
  asr: { p50: number; p95: number; p99: number }
  by_outcome: Record<string, number>
  daily: DailyPoint[]
  kpis: Kpis
  rows: StageRow[]
  runtime: { cpu: number | null; memUsed: number; memTotal: number }
  model: { name: string; quant: string; ready: boolean }
}

export interface Bootstrap {
  version: string
  dataDir: string
  settings: Settings
  models: { current: string; list: ModelInfo[] }
  correction: { available: boolean; name: string; sizeMB: number; dir: string }
  autostart: boolean
}

/* ---------- 传输层 ---------- */

/** 当前生效的传输通道，调试时 `import { transport } from "@/api"` 即可确认。 */
export type Transport = "tauri" | "http"

type InvokeFn = (command: string, args?: Record<string, unknown>, options?: unknown) => Promise<unknown>

declare global {
  interface Window {
    /** Tauri v2 开启 `app.withGlobalTauri` 后注入；故意不装 @tauri-apps/api，只用全局对象。 */
    __TAURI__?: { core?: { invoke?: InvokeFn } }
    /** Tauri v2 始终注入的内部桥；未开 withGlobalTauri 时只有它可用。 */
    __TAURI_INTERNALS__?: { invoke?: InvokeFn }
  }
}

function resolveInvoke(): InvokeFn | undefined {
  if (typeof window === "undefined") return undefined
  const viaGlobal = window.__TAURI__?.core?.invoke
  if (typeof viaGlobal === "function") return viaGlobal
  const viaInternals = window.__TAURI_INTERNALS__?.invoke
  if (typeof viaInternals === "function") return viaInternals
  return undefined
}

// 模块加载时定值：运行期切换环境（浏览器 ↔ Tauri）本来就要重新加载页面。
const invoke = resolveInvoke()
export const transport: Transport = invoke ? "tauri" : "http"

type ApiMethod =
  | "bootstrap"
  | "stats"
  | "history"
  | "history_delete"
  | "settings_save"
  | "settings_reset"
  | "theme"
  | "models_use"
  | "models_find"
  | "autostart"
  | "prune"
  | "open"
  | "check_update"

/** HTTP path ↔ IPC method 对照表，与 webgui.py 的 Api 方法一一对应。 */
const ROUTES: Record<string, { method: ApiMethod; verb: "GET" | "POST" }> = {
  "/api/bootstrap": { method: "bootstrap", verb: "GET" },
  "/api/stats": { method: "stats", verb: "GET" },
  "/api/history": { method: "history", verb: "GET" },
  "/api/history/delete": { method: "history_delete", verb: "POST" },
  "/api/settings/save": { method: "settings_save", verb: "POST" },
  "/api/settings/reset": { method: "settings_reset", verb: "POST" },
  "/api/theme": { method: "theme", verb: "POST" },
  "/api/models/use": { method: "models_use", verb: "POST" },
  "/api/models/find": { method: "models_find", verb: "POST" },
  "/api/autostart": { method: "autostart", verb: "POST" },
  "/api/prune": { method: "prune", verb: "POST" },
  "/api/open": { method: "open", verb: "POST" },
  "/api/check_update": { method: "check_update", verb: "POST" },
}

function queryString(params: Record<string, unknown>): string {
  return Object.entries(params)
    .filter(([, value]) => value !== undefined)
    .map(([key, value]) => `${key}=${encodeURIComponent(String(value))}`)
    .join("&")
}

async function httpCall<T>(path: string, verb: "GET" | "POST", params: Record<string, unknown>): Promise<T> {
  const query = queryString(params)
  // body 判空用 params 而非 query：空 patch 仍发 "{}"，与旧实现逐字节一致
  const hasBody = Object.keys(params).length > 0
  const res =
    verb === "GET"
      ? await fetch(query ? `${path}?${query}` : path)
      : await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: hasBody ? JSON.stringify(params) : undefined,
        })
  if (!res.ok) throw new Error((await res.json()).error ?? res.statusText)
  return res.json()
}

/** 统一两条通道的错误形态：IPC 侧 {ok:false,error} 也抛 Error，页面 catch 逻辑不变。 */
function unwrapIpc<T>(value: unknown): T {
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>
    if (record.ok === false) {
      throw new Error(typeof record.error === "string" ? record.error : "IPC 调用失败")
    }
    // Tauri 侧若原样透传 {"id","ok","result"} 信封，这里拆掉一层
    if (record.ok === true && "result" in record) return record.result as T
  }
  return value as T
}

async function call<T>(path: string, params: Record<string, unknown> = {}): Promise<T> {
  const route = ROUTES[path]
  if (!route) throw new Error(`未登记的端点：${path}`)
  if (invoke) return unwrapIpc<T>(await invoke("py_call", { method: route.method, params }))
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
  modelsFind: (name: string, files: string[]) => call<{ matches: string[] }>("/api/models/find", { name, files }),
  useModel: (path: string) => call<{ current: string; list: ModelInfo[] }>("/api/models/use", { path }),
  toggleAutostart: (enabled: boolean) =>
    call<{ ok: boolean; error: string }>("/api/autostart", { enabled }),
  prune: () => call<{ removed: number }>("/api/prune"),
  open: (path: string) => call<{ ok: boolean }>("/api/open", { path }),
  checkUpdate: () =>
    call<{ latest: string; current: string; update: boolean } | { error: string }>("/api/check_update"),
}

export const HOTKEY_LABEL: Record<string, string> = {
  ctrl: "Ctrl（左右均可）",
  lctrl: "左 Ctrl",
  rctrl: "右 Ctrl",
  shift: "Shift（左右均可）",
  lshift: "左 Shift",
  rshift: "右 Shift",
}

export const STRATEGY_LABEL: Record<string, string> = {
  unicode: "键盘",
  clipboard: "剪贴板",
  auto: "自动",
  none: "未注入",
}

export const MODE_LABEL: Record<string, string> = {
  ptt: "PTT 模式",
  wav: "音频文件",
  seconds: "定时录音",
  interactive: "手动录音",
}
