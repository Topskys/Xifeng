/* Python 桥的公共入口。
 *
 * 原来的 `src/api.ts` 拆成了几个模块，这里做统一出口，页面照旧写
 * `import { api, type Settings } from "@/api"`：
 *
 *   types.ts     数据契约（与 webgui.py 的返回结构逐字对应）
 *   transport.ts Tauri IPC ↔ 本地 HTTP 双通道
 *   routes.ts    端点 ↔ IPC 方法对照表
 *   client.ts    api.* 方法实现
 *   labels.ts    枚举值 → 用户可见文案
 */

export { api } from "@/api/client"
export { transport } from "@/api/transport"
export type { ApiMethod } from "@/api/routes"
export {
  HOTKEY_LABEL,
  STRATEGY_LABEL,
  MODE_LABEL,
  LANGUAGE_LABEL,
} from "@/api/labels"
export type {
  Settings,
  ModelInfo,
  HistoryRow,
  Kpis,
  StageRow,
  DailyPoint,
  StatsPayload,
  Bootstrap,
} from "@/api/types"
