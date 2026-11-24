import { AlertCircle, CheckCircle, Mic, SkipBack, X } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { api } from "@/api"
import type { StreamEnd, StreamSegment, StreamStatus } from "@/api/types"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

const STATUS_LABEL: Record<StreamStatus, string> = {
  idle: "空闲",
  listening: "监听中",
  injecting: "注入中",
  injected: "已注入",
  failed: "失败",
  cancelled: "已取消",
}

const STATUS_VARIANT: Record<StreamStatus, string> = {
  idle: "secondary",
  listening: "default",
  injecting: "default",
  injected: "success",
  failed: "destructive",
  cancelled: "outline",
}

function fmtTs(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString("zh-CN", { hour12: false })
  } catch {
    return ts.slice(11, 19)
  }
}

export function StreamingPage() {
  const [segments, setSegments] = useState<StreamSegment[]>([])
  const [status, setStatus] = useState<StreamStatus>("idle")
  const [finalText, setFinalText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const unsubRef = useRef<(() => void) | null>(null)

  const clear = useCallback(() => {
    setSegments([])
    setFinalText(null)
    setError(null)
    setStatus("idle")
  }, [])

  /** 向壳发取消：壳收到后停止后续分段注入。 */
  const cancel = useCallback(async () => {
    setError(null)
    try {
      await api.streamCancel()
    } catch (e) {
      setError(String(e))
    }
    setStatus("cancelled")
    unsubRef.current?.()
    unsubRef.current = null
  }, [])

  useEffect(() => {
    const cleanup = api.streamListen({
      onSegment: (seg) => {
        // 索引回到 1 表示壳发起了新一轮会话，清空上一轮残留
        if (seg.index === 1) {
          setSegments([])
          setFinalText(null)
          setError(null)
        }
        setError(null)
        setSegments((prev) => {
          const next = [...prev]
          const i = next.findIndex((s) => s.index === seg.index)
          if (i >= 0) next[i] = seg
          else next.push(seg)
          return next.sort((a, b) => a.index - b.index)
        })
        setStatus(seg.status)
        if (seg.final) setFinalText(seg.text)
      },
      onEnd: (end: StreamEnd) => {
        if (end.ok) {
          setStatus("injected")
          setFinalText(end.text)
        } else {
          setStatus("failed")
          setError(end.error ?? "流式结束异常")
        }
        // 保持订阅，以便下一轮按住说话时壳继续推送
      },
    })
    if (cleanup) unsubRef.current = cleanup.unsubscribe

    return () => {
      unsubRef.current?.()
      unsubRef.current = null
    }
  }, [])

  const injectedCount = segments.filter((s) => s.status === "injected").length
  const failedCount = segments.filter((s) => s.status === "failed").length

  const canCancel = status === "listening" || status === "injecting"

  return (
    <div className="p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold tracking-tight">流式识别</h2>
          <p className="text-xs text-muted-foreground">
            按住说话，壳通过 Tauri Event 推送每段识别结果；每段带索引与时间戳。
          </p>
        </div>
        <span className={cn("px-2 py-1 rounded text-xs", STATUS_VARIANT[status])}>
          {STATUS_LABEL[status]}
        </span>
      </div>

      {/* 分段气泡列表 */}
      <Card className="mb-4">
        <div className="p-4">
          {segments.length === 0 ? (
            <div className="h-48 flex items-center justify-center text-sm text-muted-foreground">
              等待按住说话……每段识别结果会实时推送到这里
            </div>
          ) : (
            <div className="space-y-2">
              {segments.map((seg) => (
                <div
                  key={seg.index}
                  className="flex items-start gap-3 p-3 rounded-xl border border-border bg-card"
                >
                  <span className="shrink-0 w-6 h-6 shrink-0 items-center justify-center rounded-full bg-secondary text-sm font-mono text-secondary-foreground">
                    {seg.index}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span>{fmtTs(seg.ts)}</span>
                      <span className="h-1 w-px bg-muted" />
                      <span>{seg.final ? "最终段" : `第 ${seg.index} 段`}</span>
                    </div>
                    <p className="mt-1 text-sm leading-relaxed">{seg.text}</p>
                    <span
                      className={cn(
                        "mt-2 inline-block px-2 py-0.5 rounded text-xs font-medium",
                        STATUS_VARIANT[seg.status],
                      )}
                    >
                      {STATUS_LABEL[seg.status]}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="border-t border-border pt-3 px-6 pb-4 flex items-center justify-between text-xs text-muted-foreground">
          <span>
            {segments.length} 段 · {injectedCount} 已注入 · {failedCount} 失败
          </span>
          {canCancel && (
            <Button variant="destructive" size="sm" onClick={cancel}>
              <X size={3} /> 取消流式
            </Button>
          )}
        </div>
      </Card>

      {/* 最终句（合并后） */}
      {finalText && (
        <Card className="mb-4">
          <div className="p-4">
            <div className="flex items-center gap-2 mb-2">
              <CheckCircle className="size-4 text-success" />
              <span className="text-sm font-semibold">最终句（合并后）</span>
            </div>
            <p className="text-sm leading-relaxed">{finalText}</p>
          </div>
        </Card>
      )}

      {/* 操作 */}
      <div className="flex items-center gap-3">
        {(status === "idle" || status === "cancelled" || status === "failed") && (
          <Button onClick={clear}>
            <Mic size={3} /> 重新开始
          </Button>
        )}
        {status === "injected" && (
          <Button onClick={clear}>
            <SkipBack size={3} /> 新一轮
          </Button>
        )}
        {segments.length > 0 && !canCancel && (
          <span className="ml-auto text-xs text-muted-foreground">
            {injectedCount} 已注入 · {failedCount} 失败
          </span>
        )}
      </div>

      {error && (
        <div className="mt-3 p-3 rounded-lg border border-destructive bg-destructive/10 text-sm text-destructive flex items-start gap-2">
          <AlertCircle className="mr-2 size-4" />
          <span>{error}</span>
        </div>
      )}
    </div>
  )
}
