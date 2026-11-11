import { Minus, Square, X } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { cn } from "@/lib/utils"

/**
 * 无边框窗口的交互件（窗口已设 `decorations: false`，系统栏没有了）。
 *
 * 2026-11-11 起不再有带文字的标题栏（参照 zcode 布局）：顶部只留一条
 * 细拖拽带 `WindowTopBar`（无标题文字），窗口按钮固定在顶条右端，
 * 页面内容从顶条下开始。
 *
 * 为什么不用 `data-tauri-drag-region`：它依赖 Tauri 注入的 CSS/JS 钩子，
 * 而本项目的窗口是 WebView2 + 自定义协议，实测该属性在按钮以外的空白处
 * 并不总是生效。这里直接调 `window.__TAURI_INTERNALS__.invoke` 的窗口命令，
 * 行为可控；拿不到 Tauri 时（浏览器里 `pnpm dev` / Edge `--app` 模式）
 * 退化为纯装饰，不报错、不渲染按钮。
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

async function invokeWindow(cmd: string): Promise<void> {
  const invoke = getInvoke()
  if (!invoke) return
  try {
    await invoke(cmd)
  } catch (e) {
    console.warn(`[titlebar] ${cmd} 失败`, e)
  }
}

/**
 * 顶部细拖拽带（参照 zcode 布局，2026-11-11）：无标题文字，整条可拖动/
 * 双击最大化，窗口按钮固定在右端。传入 `left` 时条带左段渲染品牌块
 * （宽度/背景与侧栏一致），右段才是拖拽+按钮；加载态不传，整条留白。
 */
export function WindowTopBar({ left }: { left?: React.ReactNode }) {
  const drag = useWindowDragProps()
  if (!left) {
    return (
      <div className="flex h-10 shrink-0 select-none items-center justify-end pr-2" {...drag}>
        <WindowControls />
      </div>
    )
  }
  return (
    <div className="flex h-10 shrink-0 select-none" {...drag}>
      {left}
      <div className="flex min-w-0 flex-1 items-center justify-end pr-2">
        <WindowControls />
      </div>
    </div>
  )
}

/**
 * 拖窗 / 双击最大化的 handlers：铺在「页面头部」这类空白区域上。
 * 按钮容器加 `data-no-drag` 即可豁免。
 */
export function useWindowDragProps() {
  const call = useCallback(invokeWindow, [])
  return {
    // 空白处按下即拖窗：mousedown 才触发，避免与按钮点击冲突
    onMouseDown: (e: React.MouseEvent) => {
      if (e.button !== 0) return
      if ((e.target as HTMLElement).closest("[data-no-drag]")) return
      void call("start_dragging")
    },
    onDoubleClick: (e: React.MouseEvent) => {
      if ((e.target as HTMLElement).closest("[data-no-drag]")) return
      void call("toggle_maximize")
    },
  }
}

/** 最小化 / 最大化 / 关闭 三个窗口按钮（非 Tauri 环境不渲染）。 */
export function WindowControls() {
  const [maximized, setMaximized] = useState(false)
  const tauri = isTauri()

  useEffect(() => {
    // 同步最大化状态：双击、快捷键、边缘吸附都会改变它
    if (!tauri) return
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
  }, [tauri])

  if (!tauri) return null

  return (
    <div className="flex items-center gap-0.5" data-no-drag>
      <button
        type="button"
        aria-label="最小化"
        onClick={() => void invokeWindow("minimize")}
        className="flex h-7 w-8 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        <Minus className="size-3.5" />
      </button>
      <button
        type="button"
        aria-label={maximized ? "还原" : "最大化"}
        onClick={() => {
          void invokeWindow("toggle_maximize")
          setMaximized((v) => !v)
        }}
        className="flex h-7 w-8 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        <Square className={cn("size-3", maximized && "rotate-[-90deg]")} />
      </button>
      <button
        type="button"
        aria-label="关闭"
        onClick={() => void invokeWindow("close")}
        className="flex h-7 w-8 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-destructive hover:text-white"
      >
        <X className="size-3.5" />
      </button>
    </div>
  )
}
