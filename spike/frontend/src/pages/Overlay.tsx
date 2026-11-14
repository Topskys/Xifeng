import { Mic } from "lucide-react"
import { useEffect, useState } from "react"

// 录音浮窗页（D16：webview 浮窗，Tk 截图模糊方案退役）。
// 窗口由 Rust 侧在录音会话中 show/hide；页面只负责渲染「说话中 + 音量条」。
// 音量来自 Rust 编排器每 ~40ms 发的 `voice://level` 事件（RMS 0..1）。

interface LevelEvent {
  payload: number
}

export function OverlayPage() {
  const [level, setLevel] = useState(0)

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
          setLevel(Math.max(0, Math.min(1, ev.payload)))
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

  // RMS 增益：正常说话峰值约 0.02–0.3，×4 拉满显示区间
  const pct = Math.min(100, Math.round(level * 400))

  return (
    <div className="flex h-full items-center justify-center">
      <div className="flex items-center gap-3 rounded-full border border-white/10 bg-black/80 px-5 py-2.5 shadow-2xl backdrop-blur-sm">
        {/* 应用统一图标（黑色）：与主界面品牌块同款 Mic，白色底衬托保证在深色胶囊上可辨 */}
        <span className="relative flex size-4 shrink-0 items-center justify-center">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-md bg-white/20" />
          <span className="relative inline-flex size-4 items-center justify-center rounded-md bg-white">
            <Mic className="size-2.5 text-black" />
          </span>
        </span>
        <span className="text-[13px] leading-none text-white/90">松开结束 · Esc 取消</span>
        <div className="h-1.5 w-24 overflow-hidden rounded-full bg-white/15">
          <div
            className="h-full rounded-full bg-emerald-400 transition-[width] duration-100"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
    </div>
  )
}
