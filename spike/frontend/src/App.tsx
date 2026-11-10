import { BarChart3, Info, Mic, Moon, Package, Settings, Sun } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { api, type Bootstrap } from "@/api"
import { cn } from "@/lib/utils"
import { TitleBar } from "@/components/title-bar"
import { AboutPage } from "@/pages/About"
import { ConfigPage } from "@/pages/Config"
import { ModelsPage } from "@/pages/Models"
import { StatsPage } from "@/pages/Stats"
import { Button } from "@/components/ui/button"

export type PageKey = "stats" | "models" | "config" | "history" | "about"

const NAV: { key: PageKey; label: string; icon: typeof BarChart3 }[] = [
  { key: "stats", label: "统计", icon: BarChart3 },
  { key: "models", label: "模型", icon: Package },
  { key: "config", label: "配置", icon: Settings },
  { key: "about", label: "关于", icon: Info },
]

export interface Toast {
  text: string
  ok?: boolean
}

const PAGE_KEYS = ["stats", "models", "config", "about"]

function pageFromHash(): PageKey {
  const h = location.hash.replace("#", "")
  return (PAGE_KEYS as string[]).includes(h) ? (h as PageKey) : "stats"
}

export default function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null)
  const [page, setPage] = useState<PageKey>(pageFromHash)
  const [toast, setToast] = useState<Toast | null>(null)

  const setPageHash = useCallback((key: PageKey) => {
    setPage(key)
    location.hash = key
  }, [])

  useEffect(() => {
    const onHash = () => setPage(pageFromHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  const showToast = useCallback((text: string, ok = true) => {
    setToast({ text, ok })
    window.setTimeout(() => setToast(null), 3500)
  }, [])

  const applyTheme = useCallback((theme: string) => {
    const dark = theme !== "light"
    document.documentElement.classList.toggle("dark", dark)
    // Edge 应用窗口的标题栏颜色跟随主题
    document.querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", dark ? "#0a0a0b" : "#ffffff")
  }, [])

  useEffect(() => {
    api.bootstrap().then((b) => {
      applyTheme(b.settings.theme)
      setBoot(b)
    }).catch((e) => showToast(String(e), false))
  }, [applyTheme, showToast])

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
  }, [boot, applyTheme, showToast])

  if (!boot) {
    return (
      <div className="flex h-full flex-col">
        <TitleBar />
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          正在连接本地服务…
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <TitleBar />
      <div className="flex min-h-0 flex-1">
        {/* 侧边栏 */}
        <aside className="flex w-48 shrink-0 flex-col border-r border-border bg-sidebar">
        <div className="flex items-center gap-2.5 px-5 pb-4 pt-6">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
            <Mic className="size-4 text-primary-foreground" />
          </div>
          <div>
            <div className="text-sm font-bold leading-tight">VoiceIME</div>
            <div className="text-[10px] text-muted-foreground">溪风语音输入法 · v{boot.version}</div>
          </div>
        </div>
        <nav className="flex flex-col gap-1 px-3">
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
          <ConfigPage boot={boot} showToast={showToast} refreshBoot={setBoot} applyTheme={applyTheme} />
        )}
        {page === "about" && <AboutPage boot={boot} />}
      </main>
      </div>

      {/* toast */}
      {toast && (
        <div
          className={cn(
            "fixed right-5 bottom-5 z-50 flex items-center gap-2 rounded-lg border border-border bg-popover px-4 py-2.5 text-sm shadow-lg",
            toast.ok ? "text-foreground" : "text-destructive",
          )}
        >
          <span className={cn("size-2 rounded-full", toast.ok ? "bg-success" : "bg-destructive")} />
          {toast.text}
        </div>
      )}
    </div>
  )
}
