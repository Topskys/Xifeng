/** 全局 toast 的展示层（状态由 `useToast` 管）。 */

import { cn } from "@/lib/utils"
import type { Toast } from "@/hooks/useToast"

export function ToastView({ toast }: { toast: Toast | null }) {
  if (!toast) return null
  return (
    <div
      className={cn(
        "fixed right-5 bottom-5 z-50 flex items-center gap-2 rounded-lg border border-border bg-popover px-4 py-2.5 text-sm shadow-lg",
        toast.ok ? "text-foreground" : "text-destructive",
      )}
    >
      <span className={cn("size-2 rounded-full", toast.ok ? "bg-success" : "bg-destructive")} />
      {toast.text}
    </div>
  )
}
