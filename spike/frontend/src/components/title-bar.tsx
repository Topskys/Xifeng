import { Minus, Square, X } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { cn } from "@/lib/utils"

/**
 * 自定义标题栏（窗口已设 `decorations: false`，系统栏没有了）。
 *
 * 为什么不用 `data-tauri-drag-region`：它依赖 Tauri 注入的 CSS/JS 钩子，
 * 而本项目的窗口是 WebView2 + 自定义协议，实测该属性在按钮以外的空白处
 * 并不总是生效。这里直接调 `window.__TAURI_INTERNALS__.invoke` 的窗口命令，
 * 行为可控；拿不到 Tauri 时（浏览器里 `pnpm dev`）退化为纯装饰条，不报错。
 */

type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>

function getInvoke(): Invoke | undefined {
  const w = window as unknown as {
    __TAURI__?: { core?: { invoke?: Invoke } }
    __TAURI_INTERNALS__?: { invoke?: Invoke }
  }
  return w.__TAURI__?.core?.invoke ?? w.__TAURI_INTERNALS__?.invoke
}

const isTauri = (): boolean => getInvoke() !== undefined

export function TitleBar() {
  const [maximized, setMaximized] = useState(false)
  const tauri = isTauri()

  const call = useCallback(async (cmd: string, args?: Record<string, unknown>) => {
    const invoke = getInvoke()
    if (!invoke) return
    try {
      await invoke(cmd, args)
    } catch (e) {
      console.warn(`[titlebar] ${cmd} 失败`, e)
    }
  }, [])

  useEffect(() => {
    // 同步最大化状态：双击、快捷键、边缘吸附都会改变它
    const invoke = getInvoke()
    if (!invoke) return
    const sync = async () => {
      try {
        const v = await invoke("is_maximized")
        setMaximized(Boolean(v))
      } catch {
        /* 忽略：读不到就按未最大化显示 */
      }
    }
    void sync()
    const onResize = () => void sync()
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
  }, [])

  return (
    <header
      className="flex h-9 shrink-0 select-none items-center border-b border-border bg-sidebar"
      // 空白处按下即拖窗：mousedown 才触发，避免与按钮点击冲突
      onMouseDown={(e) => {
        if (e.button !== 0) return
        if ((e.target as HTMLElement).closest("[data-no-drag]")) return
        void call("start_dragging")
      }}
      onDoubleClick={(e) => {
        if ((e.target as HTMLElement).closest("[data-no-drag]")) return
        void call("toggle_maximize").then(() => setMaximized((v) => !v))
      }}
    >
      <div className="flex items-center gap-2 px-3">
        <span className="text-[11px] font-medium tracking-wide text-muted-foreground">
          VoiceIME
        </span>
      </div>

      <div className="ml-auto flex h-full items-stretch" data-no-drag>
        <button
          type="button"
          aria-label="最小化"
          disabled={!tauri}
          onClick={() => void call("minimize")}
          className="flex w-11 cursor-pointer items-center justify-center text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-default disabled:opacity-40"
        >
          <Minus className="size-3.5" />
        </button>
        <button
          type="button"
          aria-label={maximized ? "还原" : "最大化"}
          disabled={!tauri}
          onClick={() => {
            void call("toggle_maximize")
            setMaximized((v) => !v)
          }}
          className="flex w-11 cursor-pointer items-center justify-center text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-default disabled:opacity-40"
        >
          <Square className={cn("size-3", maximized && "rotate-[-90deg]")} />
        </button>
        <button
          type="button"
          aria-label="关闭"
          disabled={!tauri}
          onClick={() => void call("close")}
          className="flex w-11 cursor-pointer items-center justify-center text-muted-foreground transition-colors hover:bg-destructive hover:text-white disabled:cursor-default disabled:opacity-40"
        >
          <X className="size-3.5" />
        </button>
      </div>
    </header>
  )
}
