# packages/ — 共享 TS/JS 包（预留）

现在只有一个 JS 包（`apps/desktop`），**故意不为了凑 workspace 造空包**。

什么时候往这里加：

| 场景 | 包名建议 |
|---|---|
| IPC 方法名 / 参数类型要在前端与测试间复用 | `@xifeng/ipc-types` |
| UI 组件被第二个页面宿主复用 | `@xifeng/ui` |
| 出现第二个壳（例如纯 WebView2 版本） | `@xifeng/shell` |

加包后 `pnpm-workspace.yaml` 的 `packages/*` 会自动纳入，无需改配置。
