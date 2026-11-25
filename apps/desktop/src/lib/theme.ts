/** 主题应用：Tailwind dark class + Edge 应用窗口标题栏颜色。 */

export function applyTheme(theme: string) {
  const dark = theme !== "light"
  document.documentElement.classList.toggle("dark", dark)
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", dark ? "#0a0a0b" : "#ffffff")
}
