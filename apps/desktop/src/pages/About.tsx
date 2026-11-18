import { ExternalLink, Mic, RefreshCw } from "lucide-react"
import { useState } from "react"

import { api, type Bootstrap } from "@/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { PageHeader } from "@/pages/Stats"

export function AboutPage({ boot }: { boot: Bootstrap }) {
  const [checking, setChecking] = useState(false)
  const [result, setResult] = useState<string | null>(null)

  const check = async () => {
    setChecking(true)
    setResult(null)
    try {
      const r = await api.checkUpdate()
      if ("error" in r) {
        setResult(`检查失败：${r.error}`)
      } else if (r.update) {
        setResult(`发现新版本 v${r.latest}（当前 v${r.current}）`)
      } else {
        setResult(`已是最新版本 v${r.current}`)
      }
    } catch (e) {
      setResult(`检查失败：${String(e)}`)
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="p-6 mx-auto w-full max-w-4xl">
      <PageHeader title="关于" desc="溪风（Xifeng）· 语音输入法" />

      <Card className="flex flex-row items-center gap-5 rounded-xl border-border p-6">
        <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-xl bg-primary">
          <Mic className="size-7 text-primary-foreground" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-lg font-bold">溪风</span>
            <Badge variant="muted">Xifeng v{boot.version}</Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            按住热键说话，松开出字，落在光标处。识别、纠错与注入全程在本机完成。
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button size="sm" variant="outline" onClick={check} disabled={checking}>
            <RefreshCw className={checking ? "animate-spin" : ""} /> 检查更新
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => api.open("https://github.com/Topskys/Xifeng")}
          >
            <ExternalLink /> GitHub
          </Button>
        </div>
      </Card>

      {result && (
        <div className="mt-3 rounded-xl border border-border bg-card px-5 py-3 text-sm">
          {result}
        </div>
      )}

      <Card className="mt-3 rounded-xl border-border p-5">
        <div className="text-sm font-semibold">简介</div>
        <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
          Xifeng（溪风）是一款 Windows 语音输入法：SenseVoice 识别、
          可选本地小模型润色整句、支持自定义术语表。音频与文本不出本机，
          唯一的网络请求是模型下载与更新检查。
        </p>
      </Card>
    </div>
  )
}
