/* 防抖保存：连续变更只在停手一段时间后落一次。
 *
 * 典型场景是设置页的滑杆/开关：拖动时每帧都 setState，但写 config.json
 * 只需要最后一次。`read` 每次渲染更新，保证定时器触发时拿到最新值。
 */

import { useCallback, useEffect, useRef } from "react"

export function useDebouncedSave<T>(
  read: () => T,
  save: (value: T) => void,
  delay = 400,
) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const readRef = useRef(read)
  const saveRef = useRef(save)

  useEffect(() => {
    readRef.current = read
    saveRef.current = save
  })

  const cancel = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  const schedule = useCallback(() => {
    cancel()
    timer.current = setTimeout(() => {
      timer.current = null
      saveRef.current(readRef.current())
    }, delay)
  }, [cancel, delay])

  // 卸载时丢弃未触发的保存，避免对已卸载页面 setState
  useEffect(() => cancel, [cancel])

  return { schedule, cancel }
}
