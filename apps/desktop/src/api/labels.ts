/* 枚举值 → 用户可见文案。
 *
 * 与 `core/xifeng/settings.py` 的常量同源：那边定义合法取值，这里定义怎么显示。
 * 新增取值时两边都要改。
 */

// 热键预设：左右互斥（2026-11-14 用户要求）——没有"左右均可"这个选项。
// 默认右 Ctrl：与 Python `hotkey.DEFAULT_KEY`、壳 `KeyPreset` 默认值一致。
export const HOTKEY_LABEL: Record<string, string> = {
  rctrl: "右 Ctrl（默认）",
  lctrl: "左 Ctrl",
  rshift: "右 Shift",
  lshift: "左 Shift",
}

export const STRATEGY_LABEL: Record<string, string> = {
  unicode: "键盘",
  clipboard: "剪贴板",
  auto: "自动",
  none: "未注入",
}

export const MODE_LABEL: Record<string, string> = {
  ptt: "PTT 模式",
  wav: "音频文件",
  seconds: "定时录音",
  interactive: "手动录音",
}

/** ASR 识别语言（SenseVoice 语种）。必须显式指定：auto 会把普通话判成粤语（G22）。 */
export const LANGUAGE_LABEL: Record<string, string> = {
  zh: "中文（普通话）",
  en: "English",
  ja: "日本語",
  ko: "한국어",
  yue: "粤语",
}
