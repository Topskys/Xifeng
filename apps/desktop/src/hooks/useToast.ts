/* 全局 toast：页面只需 `showToast(text, ok?)`，样式与自动消失统一在这里。 */

import { useCallback, useEffect, useRef, useState } from "react"

export interface Toast {
  text: string
  ok?: boolean
}

/** toast 存活时长（毫秒）。 */
const TOAST_MS = 3500

export function useToast() {
  const [toast, setToast] = useState<Toast | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const dismiss = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  const showToast = useCallback(
    (text: string, ok = true) => {
      // 连续提示时重置计时，避免上一条的定时器把新提示提前关掉
      dismiss()
      setToast({ text, ok })
      timer.current = setTimeout(() => {
        timer.current = null
        setToast(null)
      }, TOAST_MS)
    },
    [dismiss],
  )

  useEffect(() => dismiss, [dismiss])

  return { toast, showToast }
}
