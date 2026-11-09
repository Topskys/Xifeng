/* Python 桥：全部走本地 HTTP（127.0.0.1），数据不出本机。 */

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

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error((await res.json()).error ?? res.statusText)
  return res.json()
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw new Error((await res.json()).error ?? res.statusText)
  return res.json()
}

export const api = {
  bootstrap: () => get<Bootstrap>("/api/bootstrap"),
  stats: (period: string) => get<StatsPayload>(`/api/stats?period=${period}`),
  history: (period: string, offset: number, limit: number) =>
    get<{ rows: HistoryRow[]; has_more: boolean }>(
      `/api/history?period=${period}&offset=${offset}&limit=${limit}`,
    ),
  deleteHistory: (id: number) => post<{ ok: boolean }>("/api/history/delete", { id }),
  saveSettings: (patch: Partial<Settings>) => post<Settings>("/api/settings/save", patch),
  resetSettings: () => post<Settings>("/api/settings/reset"),
  setTheme: (theme: string) => post<Settings>("/api/theme", { theme }),
  modelsFind: (name: string, files: string[]) =>
    post<{ matches: string[] }>("/api/models/find", { name, files }),
    useModel: (path: string) => post<{ current: string; list: ModelInfo[] }>("/api/models/use", { path }),
  toggleAutostart: (enabled: boolean) =>
    post<{ ok: boolean; error: string }>("/api/autostart", { enabled }),
  prune: () => post<{ removed: number }>("/api/prune"),
  open: (path: string) => post<{ ok: boolean }>("/api/open", { path }),
  checkUpdate: () => post<{ latest: string; current: string; update: boolean } | { error: string }>(
    "/api/check_update",
  ),
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
