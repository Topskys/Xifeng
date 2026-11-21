/* Python 桥的数据契约：壳 ↔ Python 核之间的 JSON 结构。
 *
 * 这些接口是**契约**，不是随手写的类型：字段名必须与
 * `core/xifeng/webgui.py` 的返回结构逐字对应，改一边就要改另一边。
 * 对照表见 `docs/03-core-concepts/ipc-contract.md`。
 */

/** 用户设置（存在 `%LOCALAPPDATA%\Xifeng\config.json`，见 `core/xifeng/config.py`）。 */
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
  /** 常驻麦克风：开启后跨会话保留设备流（指示灯常亮），默认关闭 */
  resident_mic: boolean
  model_correct: boolean
  popup_color: string
  /** ASR 识别语言：zh | en | ja | ko | yue */
  language: string
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
  /** 外置配置文件路径（`%LOCALAPPDATA%\Xifeng\config.json`），用户可直接编辑 */
  configPath: string
  settings: Settings
  models: { current: string; list: ModelInfo[] }
  correction: { available: boolean; name: string; sizeMB: number; dir: string }
  autostart: boolean
}
