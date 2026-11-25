/** 展示用格式化工具（页面共享）。 */

/** 字节数 → 人类可读大小。 */
export function fmtSize(n: number) {
  if (n <= 0) return "0 MB"
  return n >= 1024 ** 3
    ? `${(n / 1024 ** 3).toFixed(1)} GB`
    : `${(n / 1024 ** 2).toFixed(0)} MB`
}
