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
  /** ITN 标点与数字改写（默认开启；中英混排时可关闭） */
  use_itn: boolean
  /** 本地纠错读取的最近成功历史轮数（0–50，默认 10；config.json 手改） */
  correction_history_limit: number
  /** 自定义术语/风格提示；不能关闭纠错安全守卫 */
  correction_prompt: string
  /** 是否把识别文本存入本地历史库 */
  store_text: boolean
  /** 流式输出：句内分段实时注入，默认关（句级输出基线） */
  streaming_enabled: boolean
}

export interface ModelInfo {
  path: string
  name: string
  /** 模型用途：ASR（语音识别）/ llm（文本润色） */
  kind?: "ASR" | "llm"
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
  models: {
    /** 当前选择：ASR 目录 / LLM gguf 文件（空串表示未选） */
    current: { asr: string; llm: string }
    list: ModelInfo[]
  }
  correction: { available: boolean; name: string; sizeMB: number; dir: string }
  autostart: boolean
}

/* ===== 流式输出（分段识别推送） =====
 * 壳通过 Tauri Event 向前端推送每段识别结果；前端只监听，不主动拉取。
 * 以下事件名与 payload 结构是前端侧的期望契约，Rust 侧（另一子 Agent）
 * 实现时应与此对齐。不涉及 Python / webgui。
 */

/** 壳 → 前端的 Tauri 事件名。 */
export const STREAM_EVENT = {
  /** 每段识别结果；payload 为 StreamSegment。 */
  segment: "voice://stream/segment",
  /** 流式结束；payload 为 StreamEnd。 */
  end: "voice://stream/end",
  /** 用户取消后壳确认；payload 为 StreamCancelled。 */
  cancelled: "voice://stream/cancelled",
} as const

/** 分段注入状态。 */
export type StreamStatus =
  | "idle" // 未开始
  | "listening" // 壳正在推送分段
  | "injecting" // 当前分段正在注入
  | "injected" // 分段已注入 / 流式已完成
  | "failed" // 分段 / 流式失败
  | "cancelled" // 用户已取消

/** 单段识别结果（壳每识别一段推送一次）。 */
export interface StreamSegment {
  /** 分段索引，从 1 开始递增 */
  index: number
  /** 该段识别文本（原始，未经后处理 / 纠错） */
  text: string
  /** 该段时间戳（ISO 8601） */
  ts: string
  /** 是否为流式的最后一段 */
  final: boolean
  /** 该段的注入状态 */
  status: StreamStatus
}

/** 流式结束事件的 payload。 */
export interface StreamEnd {
  ok: boolean
  /** 最后一段的索引 */
  index: number
  /** 后处理与纠错守卫后合并的最终完整文本（ok 为 true 时有效） */
  text: string
  /** ok 为 false 时的错误信息 */
  error: string | null
}

/** 取消确认事件的 payload。 */
export interface StreamCancelled {
  index: number
}

/** 前端侧流式会话状态快照。 */
export interface StreamState {
  status: StreamStatus
  segments: StreamSegment[]
  finalText: string | null
  error: string | null
}
