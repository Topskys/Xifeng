/* 重新拉取 bootstrap 并写回顶层状态（模型/设置变更后刷新全局快照）。 */

import { useCallback } from "react"

import { api, type Bootstrap } from "@/api"

export function useReloadBoot(refreshBoot: (b: Bootstrap) => void) {
  return useCallback(async () => {
    try {
      const boot = await api.bootstrap()
      refreshBoot(boot)
      return boot
    } catch {
      // 刷新失败保留旧快照，页面继续可用
      return null
    }
  }, [refreshBoot])
}
