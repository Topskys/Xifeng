/* 全局提示：页面只认 `showToast(text, ok?)`，底层是 shadcn 的 sonner。
 *
 * 不再自己维护 toast 列表/定时器——sonner 负责堆叠、自动消失与动画；
 * 这里只做成功/失败语义映射，页面调用方式保持不变。
 */

import { useCallback } from "react"
import { toast } from "sonner"

export function useToast() {
  const showToast = useCallback((text: string, ok = true) => {
    if (ok) toast.success(text)
    else toast.error(text)
  }, [])

  return { showToast }
}
