/** 页面与顶层共享的 props 类型。 */

import type { Bootstrap } from "@/api"

/** 统一的提示回调：ok 决定成功/失败样式。 */
export type ToastFn = (text: string, ok?: boolean) => void

/** 主界面页面的公共 props（bootstrap 快照 + 刷新入口 + 提示）。 */
export interface PageProps {
  boot: Bootstrap
  showToast: ToastFn
  refreshBoot: (b: Bootstrap) => void
}
