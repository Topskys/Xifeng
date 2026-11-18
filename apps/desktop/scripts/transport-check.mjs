/* 传输层验证（极简，不引入测试框架，不装依赖）：
 *   node scripts/transport-check.mjs
 *
 * 用例 A：window 上没有 __TAURI__ → transport === "http"，每个方法仍打原有 fetch(path)
 * 用例 B：window.__TAURI__ 存在     → transport === "tauri"，走 invoke("py_call", {method, params})，不发 fetch
 *
 * 依赖 Node 22.6+ 的 --experimental-strip-types（Node 24 默认开启），直接 import ../src/api.ts。
 */
import assert from "node:assert/strict"

// 浏览器全局垫片：api.ts 用的是 window.fetch / window.__TAURI__，node 里没有 window
globalThis.window = globalThis.window ?? {}

/* ---------------- 用例 A：无 __TAURI__，必须走 fetch ---------------- */
const calls = []
globalThis.fetch = async (path, init) => {
  calls.push({ path, method: init?.method ?? "GET", body: init?.body })
  return { ok: true, status: 200, statusText: "OK", json: async () => ({ ok: true, via: "fetch" }) }
}

const http = await import("../src/api.ts")
assert.equal(http.transport, "http", "没有 __TAURI__ 时 transport 必须是 http")

await http.api.bootstrap()
await http.api.stats("7d")
await http.api.history("all", 20, 50)
await http.api.deleteHistory(7)
await http.api.saveSettings({ theme: "dark" })
await http.api.setTheme("light")
await http.api.resetSettings()

assert.deepEqual(calls.slice(0, 3).map((c) => c.path), [
  "/api/bootstrap",
  "/api/stats?period=7d",
  "/api/history?period=all&offset=20&limit=50",
], "GET 参数必须拼进 query，与旧行为一致")
assert.deepEqual(calls[3], { path: "/api/history/delete", method: "POST", body: '{"id":7}' })
assert.deepEqual(calls[4], { path: "/api/settings/save", method: "POST", body: '{"theme":"dark"}' })
assert.deepEqual(calls[5], { path: "/api/theme", method: "POST", body: '{"theme":"light"}' })
assert.deepEqual(calls[6], { path: "/api/settings/reset", method: "POST", body: undefined }, "无参 POST 不带 body")
console.log(`✓ A 无 __TAURI__：transport=http，${calls.length} 次 fetch，path/query/body 与原实现一致`)

/* ---------------- 用例 B：有 __TAURI__，必须走 IPC ---------------- */
let fetchAfterTauri = 0
globalThis.fetch = async () => {
  fetchAfterTauri += 1
  throw new Error("Tauri 模式下不允许走 fetch")
}
const invokes = []
globalThis.window.__TAURI__ = {
  core: {
    invoke: async (cmd, args) => {
      invokes.push({ cmd, args })
      return { ok: true, via: "ipc" }
    },
  },
}

// 加 query 后缀让 Node 重新求值模块（transport 在模块加载时定值）
const tauri = await import("../src/api.ts?tauri")
assert.equal(tauri.transport, "tauri", "有 __TAURI__ 时 transport 必须是 tauri")

await tauri.api.bootstrap()
await tauri.api.stats("30d")
await tauri.api.history("week", 0, 10)
await tauri.api.deleteHistory(3)
await tauri.api.saveSettings({ theme: "dark" })
await tauri.api.resetSettings()
await tauri.api.setTheme("light")
await tauri.api.modelsFind("sensevoice", ["model.onnx"])
await tauri.api.useModel("C:/m/a")
await tauri.api.toggleAutostart(true)
await tauri.api.prune()
await tauri.api.open("__data__")
await tauri.api.checkUpdate()

assert.deepEqual(invokes.map((i) => i.args.method), [
  "bootstrap", "stats", "history", "history_delete", "settings_save", "settings_reset",
  "theme", "models_find", "models_use", "autostart", "prune", "open", "check_update",
], "path → method 映射必须与 ipc-contract.md 方法表一致")
assert.ok(invokes.every((i) => i.cmd === "py_call"), "统一走 py_call")
assert.deepEqual(invokes[2].args.params, { period: "week", offset: 0, limit: 10 })
assert.deepEqual(invokes[3].args.params, { id: 3 })
assert.equal(fetchAfterTauri, 0, "Tauri 模式下一次 fetch 都不该发生")
console.log(`✓ B 有 __TAURI__：transport=tauri，13 次 py_call，0 次 fetch`)

/* ---------------- 用例 C：IPC 侧 {ok:false} 必须抛 Error ---------------- */
globalThis.window.__TAURI__ = {
  core: { invoke: async () => ({ ok: false, error: "数据库打开失败" }) },
}
const failing = await import("../src/api.ts?tauri-fail")
await assert.rejects(() => failing.api.bootstrap(), /数据库打开失败/, "错误要和 fetch 分支同样抛 Error")
console.log("✓ C IPC 错误信封 {ok:false,error} 转成 Error，页面 catch 逻辑不用改")

/* ---------------- 用例 D：__TAURI_INTERNALS__ 兜底 ---------------- */
delete globalThis.window.__TAURI__
let internalCalls = 0
globalThis.window.__TAURI_INTERNALS__ = {
  invoke: async () => {
    internalCalls += 1
    return {}
  },
}
const internals = await import("../src/api.ts?internals")
assert.equal(internals.transport, "tauri")
await internals.api.prune()
assert.equal(internalCalls, 1)
console.log("✓ D 只有 __TAURI_INTERNALS__ 时同样判定为 tauri")

console.log("\n全部通过")
