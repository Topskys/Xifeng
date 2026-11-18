/* 传输层：Tauri IPC ↔ 本地 HTTP 双通道。
 *
 * 传输在模块加载时定一次，api 的方法签名/返回值不变，页面组件零改动：
 *   - 有 window.__TAURI__（或 window.__TAURI_INTERNALS__）→ core.invoke("py_call", { method, params })
 *   - 否则 → fetch(path)，与旧实现完全一致
 * 映射关系见 docs/03-core-concepts/ipc-contract.md 的方法表。
 */

/** 当前生效的传输通道，调试时 `import { transport } from "@/api/transport"` 即可确认。 */
export type Transport = "tauri" | "http"

type InvokeFn = (command: string, args?: Record<string, unknown>, options?: unknown) => Promise<unknown>

declare global {
  interface Window {
    /** Tauri v2 开启 `app.withGlobalTauri` 后注入；故意不装 @tauri-apps/api，只用全局对象。 */
    __TAURI__?: { core?: { invoke?: InvokeFn } }
    /** Tauri v2 始终注入的内部桥；未开 withGlobalTauri 时只有它可用。 */
    __TAURI_INTERNALS__?: { invoke?: InvokeFn }
  }
}

function resolveInvoke(): InvokeFn | undefined {
  if (typeof window === "undefined") return undefined
  const viaGlobal = window.__TAURI__?.core?.invoke
  if (typeof viaGlobal === "function") return viaGlobal
  const viaInternals = window.__TAURI_INTERNALS__?.invoke
  if (typeof viaInternals === "function") return viaInternals
  return undefined
}

// 模块加载时定值：运行期切换环境（浏览器 ↔ Tauri）本来就要重新加载页面。
const invoke = resolveInvoke()
export const transport: Transport = invoke ? "tauri" : "http"

/** 传输通道内部用的原始调用器（未定义 = 纯 HTTP 模式）。 */
export const tauriInvoke = invoke

function queryString(params: Record<string, unknown>): string {
  return Object.entries(params)
    .filter(([, value]) => value !== undefined)
    .map(([key, value]) => `${key}=${encodeURIComponent(String(value))}`)
    .join("&")
}

export async function httpCall<T>(
  path: string,
  verb: "GET" | "POST",
  params: Record<string, unknown>,
): Promise<T> {
  const query = queryString(params)
  // body 判空用 params 而非 query：空 patch 仍发 "{}"，与旧实现逐字节一致
  const hasBody = Object.keys(params).length > 0
  const res =
    verb === "GET"
      ? await fetch(query ? `${path}?${query}` : path)
      : await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: hasBody ? JSON.stringify(params) : undefined,
        })
  if (!res.ok) throw new Error((await res.json()).error ?? res.statusText)
  return res.json()
}

/** 统一两条通道的错误形态：IPC 侧 {ok:false,error} 也抛 Error，页面 catch 逻辑不变。 */
export function unwrapIpc<T>(value: unknown): T {
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>
    if (record.ok === false) {
      throw new Error(typeof record.error === "string" ? record.error : "IPC 调用失败")
    }
    // Tauri 侧若原样透传 {"id","ok","result"} 信封，这里拆掉一层
    if (record.ok === true && "result" in record) return record.result as T
  }
  return value as T
}
