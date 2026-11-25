/* 页面通用版式：标题头、设置行、设置分组、小节标签。
 *
 * 这些结构原来散落在 Stats / Config / Models 里互相 import，属于跨页面
 * 复用件，统一收进 components 避免页面之间产生依赖。
 */

import type React from "react"

import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

/** 页面标题：左侧标题 + 说明，右侧放操作按钮。 */
export function PageHeader({
  title,
  desc,
  children,
}: {
  title: string
  desc?: string
  children?: React.ReactNode
}) {
  return (
    <div className="mb-5 flex items-end justify-between">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
        {desc && <p className="mt-1 text-xs text-muted-foreground">{desc}</p>}
      </div>
      {children && <div className="flex items-center gap-2">{children}</div>}
    </div>
  )
}

/** 设置行：左侧标题/说明，右侧控件。 */
export function Row({
  title,
  desc,
  children,
}: {
  title: string
  desc?: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-3.5">
      <div className="min-w-0">
        <div className="text-sm font-medium">{title}</div>
        {desc && <div className="mt-1 text-xs text-muted-foreground">{desc}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

/** 设置分组：一张卡片内用分隔线切分多行。 */
export function Group({ children }: { children: React.ReactNode }) {
  return (
    <Card className="rounded-xl border-border px-5 py-1.5 mt-3">
      <div className="divide-y divide-border/60">{children}</div>
    </Card>
  )
}

/** 小节标签（纯文字，不抢视觉）。 */
export function SectionLabel({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        "mb-1 mt-1 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70",
        className,
      )}
    >
      {children}
    </div>
  )
}
