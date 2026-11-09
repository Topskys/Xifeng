import { FolderOpen, Trash2 } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { api, HOTKEY_LABEL, type Bootstrap, type Settings } from "@/api"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { SimpleSelect } from "@/components/simple-select"
import { Slider } from "@/components/ui/slider"
import { Switch } from "@/components/ui/switch"
import { PageHeader } from "@/pages/Stats"

export function Row({
  title,
  desc,
  children,
}: {
  title: string
  desc?: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-3.5">
      <div className="min-w-0">
        <div className="text-sm font-medium">{title}</div>
        {desc && <div className="mt-1 text-xs text-muted-foreground">{desc}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

export function Group({ children }: { children: React.ReactNode }) {
  return (
    <Card className="rounded-xl border-border px-5 py-1.5 mt-3">
      <div className="divide-y divide-border/60">{children}</div>
    </Card>
  )
}

function Segmented({
  options,
  value,
  onChange,
}: {
  options: { label: string; value: string }[]
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="flex rounded-md border border-border p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={
            "cursor-pointer rounded-sm px-3 py-1 text-xs transition-colors " +
            (value === o.value
              ? "bg-secondary font-medium text-secondary-foreground"
              : "text-muted-foreground hover:text-foreground")
          }
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

const sel = (pairs: Record<string, string>) =>
  Object.entries(pairs).map(([value, label]) => ({ value, label }))

export function ConfigPage({
  boot,
  showToast,
  refreshBoot,
  applyTheme,
}: {
  boot: Bootstrap
  showToast: (text: string, ok?: boolean) => void
  refreshBoot: (b: Bootstrap) => void
  applyTheme: (theme: string) => void
}) {
  const [s, setS] = useState<Settings>(boot.settings)
  const [dirty, setDirty] = useState(false)

  useEffect(() => setS(boot.settings), [boot.settings])

  const patch = useCallback((p: Partial<Settings>) => {
    setS((prev) => ({ ...prev, ...p }))
    setDirty(true)
  }, [])

  const save = async () => {
    try {
      const next = await api.saveSettings(s)
      const themeChanged = next.theme !== boot.settings.theme
      refreshBoot({ ...boot, settings: next })
      if (themeChanged) applyTheme(next.theme)
      setDirty(false)
      showToast(themeChanged ? "已保存 · 主题已切换" : "已保存 · 托盘重启后生效")
    } catch (e) {
      showToast(String(e), false)
    }
  }

  const reset = async () => {
    if (!window.confirm("把设置恢复为默认值？")) return
    const next = await api.resetSettings()
    setS(next)
    applyTheme(next.theme)
    refreshBoot({ ...boot, settings: next })
    setDirty(false)
    showToast("已恢复默认值 · 托盘重启后生效")
  }

  return (
    <div className="p-6 mx-auto w-full max-w-4xl">
      <PageHeader title="配置" desc="保存在本机 SQLite">
        <Button size="sm" variant="outline" onClick={reset}>
          恢复默认
        </Button>
        <Button size="sm" variant="default" onClick={save} className={dirty ? "" : "opacity-70"}>
          保存
        </Button>
      </PageHeader>

      <div>
        <Group>
          <Row title="触发热键" desc="全局键，左右两侧可分别指定">
            <SimpleSelect
              options={sel(HOTKEY_LABEL)}
              value={s.hotkey}
              onValueChange={(v) => patch({ hotkey: v })}
            />
          </Row>
          <Row title="录音段切分" desc="对按住说话的录音做 VAD 双阈值切分与无效段过滤；仍需按住热键，不是免按键">
            <Segmented
              options={[
                { label: "关闭", value: "ptt" },
                { label: "开启", value: "vad" },
              ]}
              value={s.use_vad ? "vad" : "ptt"}
              onChange={(v) => patch({ use_vad: v === "vad" })}
            />
          </Row>
        </Group>

        <Group>
          <Row title="界面主题">
            <Segmented
              options={[
                { label: "深色", value: "dark" },
                { label: "浅色", value: "light" },
              ]}
              value={s.theme}
              onChange={(v) => patch({ theme: v })}
            />
          </Row>
          <Row title="录音指示浮窗" desc="按住说话时屏幕下方显示录音状态与音量">
            <Switch checked={s.show_popup} onCheckedChange={(v) => patch({ show_popup: v })} />
          </Row>
          <Row title="ASR 推理线程" desc="8 核 CPU 实测 8 线程最快">
            <SimpleSelect
              options={sel({ "1": "1", "2": "2", "4": "4", "8": "8", "16": "16", "32": "32" })}
              value={String(s.threads)}
              onValueChange={(v) => patch({ threads: Number(v) })}
            />
          </Row>
        </Group>

        <Group>
          <Row title="模型下载源" desc="模型文件需手动放置">
            <SimpleSelect
              options={sel({ modelscope: "ModelScope", "hf-mirror": "HuggingFace 镜像", github: "GitHub Release", local: "本地目录" })}
              value={s.download_source}
              onValueChange={(v) => patch({ download_source: v })}
            />
          </Row>
          <Row title="语气词清理" desc="标准剔除句首、句中填充词，深度连句尾助词一起删">
            <Segmented
              options={[
                { label: "关闭", value: "off" },
                { label: "标准", value: "standard" },
                { label: "深度", value: "deep" },
              ]}
              value={s.filler_level}
              onChange={(v) => patch({ filler_level: v })}
            />
          </Row>
          <Row
            title="模型纠错"
            desc={
              boot.correction.available
                ? "启用本地 Qwen2.5-0.5B 自动优化会有延迟+0.5–1.5s"
                : `纠错模型缺失：把 qwen2.5-0.5b-instruct-q4_k_m.gguf 放到 ${boot.correction.dir}，重启托盘后可用`
            }
          >
              <Switch
                checked={s.model_correct}
                disabled={!boot.correction.available}
                onCheckedChange={(v) => patch({ model_correct: v })}
              />
          </Row>

          <Row title="浮窗音效颜色" desc="录音指示浮窗的图标底色与音柱颜色">
            <SimpleSelect
              options={sel({ red: "录音红", cyan: "电光青", green: "森林绿", white: "月光白" })}
              value={s.popup_color ?? "red"}
              onValueChange={(v) => patch({ popup_color: v })}
            />
          </Row>
        </Group>

        <Group>
          <Row title="注入方式" desc="自动，先发按键，确认没打进去才走剪贴板">
            <SimpleSelect
              options={sel({ auto: "自动（键盘 / 剪贴板）", unicode: "仅键盘事件", clipboard: "仅剪贴板" })}
              value={s.inject_strategy}
              onValueChange={(v) => patch({ inject_strategy: v })}
            />
          </Row>
          <Row title="焦点恢复" desc="注入前发现焦点被切走时，尝试抢回原窗口">
            <Switch checked={s.restore_focus} onCheckedChange={(v) => patch({ restore_focus: v })} />
          </Row>
          <Row title="剪贴板恢复" desc={`兜底后 ${s.clipboard_restore_ms}ms 恢复原内容（太短会粘出旧内容）`}>
            <div className="flex w-44 items-center gap-2">
              <Slider
                min={100}
                max={2000}
                step={100}
                value={[Math.max(100, s.clipboard_restore_ms)]}
                onValueChange={(v) => patch({ clipboard_restore_ms: v[0] })}
              />
              <span className="w-12 text-right text-xs tabular-nums">{s.clipboard_restore_ms}ms</span>
            </div>
          </Row>
          <Row title="句末标点">
            <Segmented
              options={[
                { label: "保留", value: "keep" },
                { label: "去掉", value: "strip" },
              ]}
              value={s.final_punct}
              onChange={(v) => patch({ final_punct: v })}
            />
          </Row>
          <Row title="中英文间距" desc="在中文与英文、数字之间补一个空格">
            <Switch
              checked={s.space_between_cjk_latin}
              onCheckedChange={(v) => patch({ space_between_cjk_latin: v })}
            />
          </Row>
          <Row title="首尾静音裁剪" desc="去掉句首句尾静音，减少幻觉与 ASR 耗时">
            <Switch checked={s.trim_silence} onCheckedChange={(v) => patch({ trim_silence: v })} />
          </Row>
        </Group>

        <Group>
          <Row title="开机自启" desc="登录后自动以托盘模式运行">
            <Switch
              checked={boot.autostart}
              onCheckedChange={async (v) => {
                const r = await api.toggleAutostart(v)
                if (r.ok) {
                  refreshBoot({ ...boot, autostart: v })
                  showToast(v ? "已开启开机自启" : "已关闭开机自启")
                } else {
                  showToast(r.error || "设置失败", false)
                }
              }}
            />
          </Row>
          <Row title="本地数据" desc={boot.dataDir}>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => api.open("__data__")}>
                <FolderOpen /> 打开
              </Button>
              <Button
                size="sm"
                variant="destructive"
                onClick={async () => {
                  if (!window.confirm("删除 30 天前的历史记录？")) return
                  const r = await api.prune()
                  showToast(`已删除 ${r.removed} 条历史`)
                }}
              >
                <Trash2 /> 清理 30 天前
              </Button>
            </div>
          </Row>
        </Group>
      </div>
    </div>
  )
}
