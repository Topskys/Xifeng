import { BarChart3, Info, Mic, Moon, Package, Settings, Sun } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { api, type Bootstrap } from "@/api"
import { Toaster } from "@/components/ui/sonner"
import { useToast } from "@/hooks/useToast"
import { applyTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"
import { WindowTopBar } from "@/components/title-bar"
import { AboutPage } from "@/pages/About"
import { ConfigPage } from "@/pages/Config"
import { ModelsPage } from "@/pages/Models"
import { OverlayPage } from "@/pages/Overlay"
import { StatsPage } from "@/pages/Stats"
import { Button } from "@/components/ui/button"

export type PageKey = "stats" | "models" | "config" | "about"

const NAV: { key: PageKey; label: string; icon: typeof BarChart3 }[] = [
  { key: "stats", label: "统计", icon: BarChart3 },
  { key: "models", label: "模型", icon: Package },
  { key: "config", label: "配置", icon: Settings },
  { key: "about", label: "关于", icon: Info },
]

const PAGE_KEYS = ["stats", "models", "config", "about"]

function pageFromHash(): PageKey {
  const h = location.hash.replace("#", "")
  return (PAGE_KEYS as string[]).includes(h) ? (h as PageKey) : "stats"
}

/** 是不是浮窗模式（`?window=overlay`）。 */
function isOverlayMode(): boolean {
  return new URLSearchParams(window.location.search).get("window") === "overlay"
}

/**
 * 顶层分发：浮窗页与主界面是**两套完全独立的 UI**（不同 webview 实例、
 * 不同路由形态），没有共享状态。
 *
 * 为什么拆成两个组件而不是在 `App` 里提前 return：
 * 提前 return 会让后面的 Hook 变成**条件调用**——同一个组件这次渲染调 3 个
 * Hook、下次渲染调 0 个，Hook 顺序变了，React 内部状态就错位
 * （`react-hooks/rules-of-hooks` 报的正是这个）。
 *
 * 拆开后：`App` 自身一个 Hook 都没有，`MainShell` 里的 Hook 全部无条件执行，
 * 顺序永远一致——规则与运行时行为都对上了。
 */
export default function App() {
  // 浮窗模式：独立精简页（录音提示条），无顶条/侧栏，背景透明
  if (isOverlayMode()) {
    return <OverlayPage />
  }
  return <MainShell />
}

/** 主界面（侧栏 + 内容区 + toast）。所有 Hook 在这里无条件调用。 */
function MainShell() {
  const [boot, setBoot] = useState<Bootstrap | null>(null)
  const [page, setPage] = useState<PageKey>(pageFromHash)
  const { showToast } = useToast()

  const setPageHash = useCallback((key: PageKey) => {
    setPage(key)
    location.hash = key
  }, [])

  useEffect(() => {
    const onHash = () => setPage(pageFromHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  useEffect(() => {
    api.bootstrap().then((b) => {
      applyTheme(b.settings.theme)
      setBoot(b)
    }).catch((e) => showToast(String(e), false))
  }, [showToast])

  // G7 反馈链：注入落在提权（管理员）窗口时 SendInput 无效，壳发事件——
  // 这里弹提示让用户知道原因，而不是静默丢字。
  useEffect(() => {
    const tauri = (window as unknown as {
      __TAURI__?: { event?: { listen: (e: string, h: (ev: { payload: unknown }) => void) => Promise<() => void> } }
    }).__TAURI__
    if (!tauri?.event?.listen) return
    let un: (() => void) | undefined
    let cancelled = false
    tauri.event
      .listen("voice://elevated", () => {
        showToast("目标窗口以管理员权限运行，收不到语音输入。请以管理员身份重启 Xifeng，或换普通权限窗口。", false)
      })
      .then((u) => {
        if (cancelled) u()
        else un = u
      })
    return () => {
      cancelled = true
      un?.()
    }
  }, [showToast])

  // 心跳：窗口关掉后本地服务 30s 内退出，避免托盘反复开窗留下僵尸进程
  useEffect(() => {
    const ping = () => {
      fetch("/api/ping", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .catch(() => {})
    }
    ping()
    const timer = window.setInterval(ping, 5000)
    return () => window.clearInterval(timer)
  }, [])

  const toggleTheme = useCallback(async () => {
    if (!boot) return
    const next = boot.settings.theme === "light" ? "dark" : "light"
    const s = await api.setTheme(next)
    applyTheme(s.theme)
    setBoot({ ...boot, settings: s })
    showToast(next === "dark" ? "已切换到深色" : "已切换到浅色")
  }, [boot, showToast])

  if (!boot) {
    return (
      <div className="flex h-full flex-col">
        <WindowTopBar />
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          正在连接本地服务…
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <WindowTopBar
        left={
          /* 顶条左段：品牌块，宽度/边线/背景与侧栏一致（zcode 同款布局） */
          <div className="flex w-48 shrink-0 items-center gap-2.5 border-r border-border bg-sidebar px-4">
            <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary">
              <Mic className="size-3.5 text-primary-foreground" />
            </div>
            <div className="min-w-0">
              <div className="text-[13px] font-bold leading-tight">Xifeng</div>
              <div className="truncate text-[9px] leading-tight text-muted-foreground">
                溪风语音输入法 · v{boot.version}
              </div>
            </div>
          </div>
        }
      />
      <div className="flex min-h-0 flex-1">
        {/* 侧边栏（品牌块已在顶条左段） */}
        <aside className="flex w-48 shrink-0 flex-col border-r border-border bg-sidebar">
        <nav className="flex flex-col gap-1 px-3 pt-2">
          {NAV.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setPageHash(key)}
              className={cn(
                "flex cursor-pointer items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
                page === key
                  ? "bg-accent font-semibold text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/60 hover:text-accent-foreground",
              )}
            >
              <Icon className="size-4" />
              {label}
            </button>
          ))}
        </nav>
        <div className="mt-auto px-3 pb-4">
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-start text-muted-foreground"
            onClick={toggleTheme}
          >
            {boot.settings.theme === "light" ? (
              <Moon className="size-4" />
            ) : (
              <Sun className="size-4" />
            )}
            {boot.settings.theme === "light" ? "深色模式" : "浅色模式"}
          </Button>
        </div>
      </aside>

      {/* 内容 */}
      <main className="min-w-0 flex-1 overflow-y-auto">
        {page === "stats" && <StatsPage showToast={showToast} />}
        {page === "models" && <ModelsPage boot={boot} showToast={showToast} refreshBoot={setBoot} />}
        {page === "config" && (
          <ConfigPage boot={boot} showToast={showToast} refreshBoot={setBoot} />
        )}
        {page === "about" && <AboutPage boot={boot} showToast={showToast} />}
      </main>
      </div>

      {/* 全局提示（sonner） */}
      <Toaster
        position="bottom-right"
        duration={3500}
        theme={boot.settings.theme === "light" ? "light" : "dark"}
      />
    </div>
  )
}
