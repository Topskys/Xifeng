import { Mic } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { api } from "@/api"

// 录音浮窗页（D16：webview 浮窗，Tk 截图模糊方案退役）。
// 窗口由 Rust 侧在录音会话中 show/hide。
// 视觉沿用旧 Tk 浮窗（popup.py 2026-10-30 改版）的「镜像音柱」动效：
// 30 根竖线、中间高两头低包络、滚动历史、安静时匀速呼吸、
// 大声段电光青→冰白双段渐变；令牌与 ui.py DARK/LIGHT 同源。
// 左边图标按用户要求改为黑色；面板圆角 6px；支持黑/白两种主题——
// 主题来自壳的 `voice://theme` 事件（会话开始/设置热更时发），
// 页面加载时再 bootstrap 兜底一次；浏览器预览可用 &theme=light 覆盖。
// 音量来自 Rust 编排器每 ~40ms 发的 `voice://level` 事件（RMS 0..1）。

type ThemeName = "dark" | "light"

const BARS = 30
// ui.py 设计令牌（旧 popup._Glass 同源）；ICE 为旧版大声段冰白
const PALETTES: Record<ThemeName, { panel: string; border: string; track: string; accent: string }> = {
  dark: {
    panel: "rgba(17, 17, 19, 0.67)", // INPUT_BG 深色蒙层（旧 tint 17,17,19,172）
    border: "#26262a", // DARK.BORDER
    track: "#1c1c1f", // DARK.DISABLED_TRACK：静止音柱
    accent: "#22d3ee", // DARK.WAVE：电光青
  },
  light: {
    panel: "rgba(250, 250, 250, 0.66)", // 旧浅色 tint 250,250,250,168
    border: "#e4e4e7", // LIGHT.BORDER
    track: "#e4e4e7", // LIGHT.DISABLED_TRACK
    accent: "#0891b2", // LIGHT.WAVE
  },
}
const ICE = "#cffafe" // 大声段的冰白（旧版两主题共用）

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

function themeFromParam(): ThemeName | null {
  const v = new URLSearchParams(window.location.search).get("theme")
  return v === "light" ? "light" : v === "dark" ? "dark" : null
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
  // 主题：URL 参数（浏览器预览）优先，否则等 bootstrap/壳事件
  const [theme, setTheme] = useState<ThemeName>(() => themeFromParam() ?? "dark")
  const paletteRef = useRef(PALETTES[theme])
  paletteRef.current = PALETTES[theme]

  useEffect(() => {
    // 浮窗要透出桌面：页面背景必须透明
    document.documentElement.style.background = "transparent"
    document.body.style.background = "transparent"

    const tauri = (window as unknown as {
      __TAURI__?: {
        event?: {
          listen: (e: string, h: (ev: { payload: unknown }) => void) => Promise<() => void>
        }
      }
    }).__TAURI__
    if (!tauri?.event?.listen) return
    let un: (() => void) | undefined
    let cancelled = false
    const listen = (event: string, handler: (ev: { payload: unknown }) => void) =>
      tauri.event!.listen!(event, handler).then((u) => {
        if (cancelled) u()
        else un = un ? wrap(un, u) : u
      })
    const wrap = (a: () => void, b: () => void) => () => {
      a()
      b()
    }
    listen("voice://level", (ev) => {
      if (typeof ev.payload === "number" && Number.isFinite(ev.payload)) {
        levelRef.current = Math.max(0, Math.min(1, ev.payload))
      }
    })
    // 壳在会话开始/设置热更时广播主题（payload: "dark" | "light"）
    listen("voice://theme", (ev) => {
      if (ev.payload === "light" || ev.payload === "dark") setTheme(ev.payload)
    })
    return () => {
      cancelled = true
      un?.()
    }
  }, [])

  // 页面加载兜底：壳事件万一错过（页面晚于事件加载），从设置里读一次。
  // demo/浏览器预览没有核心，失败保持当前主题。
  useEffect(() => {
    if (themeFromParam()) return
    api
      .bootstrap()
      .then((b) => {
        if (b.settings.theme === "light") setTheme("light")
      })
      .catch(() => {})
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
      const { track, accent } = paletteRef.current
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
          t > 0.7 ? blend(accent, ICE, (t - 0.7) / 0.3) : blend(track, accent, t / 0.7)
      }
    }, 33)
    return () => window.clearInterval(timer)
  }, [])

  const pal = PALETTES[theme]
  return (
    <div className="flex h-full items-center justify-center">
      {/* 旧 popup 几何：300×32 条、圆角 6px（用户指定）、PAD 5、图标 22、图标→音柱 10。
          胶囊 MUST 定宽——不定宽 flex-1 会塌成内容宽，音柱挤成一坨 */}
      <div
        className="flex h-8 w-[300px] items-center gap-2.5 rounded-[6px] px-[5px] backdrop-blur-sm"
        style={{ background: pal.panel, border: `1px solid ${pal.border}` }}
      >
        {/* 黑色图标（用户指定）：白麦克风保持可辨，动效全部交给右侧音柱 */}
        <span className="flex size-[22px] shrink-0 items-center justify-center rounded-[6px] bg-black">
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
              style={{ height: "3px", background: pal.track }}
            />
          ))}
        </div>
      </div>
    </div>
  )
}
