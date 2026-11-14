import { Mic } from "lucide-react"
import { useEffect, useRef } from "react"

// 录音浮窗页（D16：webview 浮窗，Tk 截图模糊方案退役）。
// 窗口由 Rust 侧在录音会话中 show/hide。
// 视觉沿用旧 Tk 浮窗（popup.py 2026-10-30 改版）的「镜像音柱」动效：
// 30 根竖线、中间高两头低包络、滚动历史、安静时匀速呼吸、
// 大声段电光青→冰白双段渐变；令牌与 ui.py DARK 同源。
// 左边图标按用户要求改为黑色（旧版是蓝底/主题色块）。
// 音量来自 Rust 编排器每 ~40ms 发的 `voice://level` 事件（RMS 0..1）。

interface LevelEvent {
  payload: number
}

const BARS = 30
// ui.py DARK 设计令牌（旧 popup._Glass 同源）
const TRACK = "#1c1c1f" // DISABLED_TRACK：静止音柱
const ACCENT = "#22d3ee" // WAVE：电光青（popup_color=red 的 accent 同为它）
const ICE = "#cffafe" // 大声段的冰白

/** 音柱高度包络：中间高、两头低，像一段波形而不是一堵墙（popup.envelope 同式）。 */
function envelope(i: number): number {
  if (BARS <= 1) return 1.0
  return 0.35 + 0.65 * Math.sin((Math.PI * i) / (BARS - 1))
}

/** 线性混合 #rrggbb（t=0 取 a，t=1 取 b；popup.blend 同式）。 */
function blend(a: string, b: string, t: number): string {
  const pa = [1, 3, 5].map((k) => parseInt(a.slice(k, k + 2), 16))
  const pb = [1, 3, 5].map((k) => parseInt(b.slice(k, k + 2), 16))
  const c = pa.map((x, k) => Math.round(x + (pb[k] - x) * t))
  return "#" + c.map((x) => x.toString(16).padStart(2, "0")).join("")
}

/** 预览用合成电平：词组起伏（说-停-说）× 音节快抖，肉眼就是正常说话。 */
function demoLevel(t: number): number {
  const phrase = Math.max(0, Math.sin(t * 1.1))
  const syllable = 0.55 + 0.45 * Math.sin(t * 9.0)
  return Math.min(1, phrase * syllable * 1.2)
}

export function OverlayPage() {
  const barsRef = useRef<(HTMLDivElement | null)[]>([])
  const levelRef = useRef(0)
  const peakRef = useRef(0)
  const histRef = useRef(new Float32Array(BARS))
  const startRef = useRef(performance.now())
  // 样式预览：?window=overlay&demo=1 时用合成电平序列代替真实麦克风事件
  // （调样式不用真说话；正式路径完全不受影响）
  const demoRef = useRef(
    new URLSearchParams(window.location.search).get("demo") === "1",
  )

  useEffect(() => {
    // 浮窗要透出桌面：页面背景必须透明
    document.documentElement.style.background = "transparent"
    document.body.style.background = "transparent"

    const tauri = (window as unknown as {
      __TAURI__?: { event?: { listen: (e: string, h: (ev: LevelEvent) => void) => Promise<() => void> } }
    }).__TAURI__
    if (!tauri?.event?.listen) return
    let un: (() => void) | undefined
    let cancelled = false
    tauri.event
      .listen("voice://level", (ev) => {
        if (typeof ev.payload === "number" && Number.isFinite(ev.payload)) {
          levelRef.current = Math.max(0, Math.min(1, ev.payload))
        }
      })
      .then((u) => {
        if (cancelled) u()
        else un = u
      })
    return () => {
      cancelled = true
      un?.()
    }
  }, [])

  useEffect(() => {
    // 动效循环（~30fps，与旧 popup.tick 的 33ms 节流一致）：直接改 DOM，
    // 不过 React 重渲染 —— 音柱每帧都在动，ref 更新最省。
    const hist = histRef.current
    const timer = window.setInterval(() => {
      if (demoRef.current) {
        levelRef.current = demoLevel((performance.now() - startRef.current) / 1000)
      }
      // 旧 _read_level 同参：RMS×6 拉进可视区间 + 慢峰值衰减（柱子不乱跳）
      const level = Math.min(1, levelRef.current * 6)
      peakRef.current = Math.max(level, peakRef.current * 0.85)
      hist.copyWithin(0, 1)
      hist[BARS - 1] = peakRef.current
      // 安静时给一点匀速呼吸，避免「卡住了」的错觉（旧 _draw_bars 同式）
      const breath = 0.1 + 0.06 * Math.sin((performance.now() - startRef.current) * 4.2)
      const maxHalf = 12 // 旧 WAVE_CY - 4：32px 高的条，柱子最长 27px
      for (let i = 0; i < BARS; i++) {
        const el = barsRef.current[i]
        if (!el) continue
        const env = envelope(i)
        const v = Math.max(hist[i], breath * env)
        const half = 1.5 + maxHalf * env * v
        el.style.height = `${Math.round(half * 2)}px`
        const t = Math.min(1, v * env * 1.35)
        // 双段渐变：底色→电光青，大声段再 → 冰白
        el.style.background =
          t > 0.7 ? blend(ACCENT, ICE, (t - 0.7) / 0.3) : blend(TRACK, ACCENT, t / 0.7)
      }
    }, 33)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <div className="flex h-full items-center justify-center">
      {/* 旧 popup 几何：300×32 条、圆角 10、PAD 5、图标 22、图标→音柱 10。
          胶囊 MUST 定宽——不定宽 flex-1 会塌成内容宽，音柱挤成一坨 */}
      <div className="flex h-8 w-[300px] items-center gap-2.5 rounded-[10px] border border-[#26262a] bg-[#111113]/[.67] px-[5px] backdrop-blur-sm">
        {/* 黑色图标（用户指定）：白麦克风保持可辨，动效全部交给右侧音柱 */}
        <span className="flex size-[22px] shrink-0 items-center justify-center rounded-[5px] bg-black">
          <Mic className="size-3 text-white" />
        </span>
        <div className="flex h-full flex-1 items-center justify-between">
          {Array.from({ length: BARS }, (_, i) => (
            <div
              key={i}
              ref={(el) => {
                barsRef.current[i] = el
              }}
              className="w-[2px] rounded-full"
              style={{ height: "3px", background: TRACK }}
            />
          ))}
        </div>
      </div>
    </div>
  )
}
