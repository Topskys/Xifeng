import { FolderOpen, Plus, Download, Loader2, X } from "lucide-react";
import { useRef, useState } from "react";

import { api, type Bootstrap } from "@/api";
import type { ModelInfo } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/pages/Stats";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

function fmtSize(n: number) {
  if (n <= 0) return "0 MB";
  return n >= 1024 ** 3
    ? `${(n / 1024 ** 3).toFixed(1)} GB`
    : `${(n / 1024 ** 2).toFixed(0)} MB`;
}

/** 模型分类：ASR（语音识别） / llm（文本润色）。 */
function modelKind(m: ModelInfo): "ASR" | "llm" {
  if (m.kind) return m.kind;
  const n = m.name.toLowerCase();
  if (n.includes("qwen") || n.includes("llm") || n.includes("0.5b") || n.includes("1.5b")) {
    return "llm";
  }
  return "ASR";
}

function modelDesc(m: ModelInfo): string {
  const n = m.name.toLowerCase();
  if (n.includes("sensevoice")) return "中英日粤韩 · 精度高速度快";
  if (n.includes("paraformer")) return "轻量流式识别";
  if (n.includes("firered")) return "更高精度中文识别";
  if (n.includes("0.5b")) return "智能标点与口语转书面语";
  if (n.includes("1.5b")) return "更强的润色与纠错能力";
  return "本地模型";
}

/** 小 tag（灰色、低调）。 */
function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[11px] leading-none text-muted-foreground">
      {children}
    </span>
  );
}

export function ModelsPage({
  boot,
  showToast,
  refreshBoot,
}: {
  boot: Bootstrap;
  showToast: (text: string, ok?: boolean) => void;
  refreshBoot: (b: Bootstrap) => void;
}) {
  const { models } = boot;
  const asrModels = models.list.filter((m) => modelKind(m) === "ASR");
  const llmModels = models.list.filter((m) => modelKind(m) === "llm");

  const [downloading, setDownloading] = useState(false);
  const [downloadJobId, setDownloadJobId] = useState<string | null>(null);
  const [downloadPct, setDownloadPct] = useState<number | null>(null);
  const jobRef = useRef<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const refresh = () =>
    api
      .bootstrap()
      .then(refreshBoot)
      .catch(() => {});

  const fileInput = useRef<HTMLInputElement>(null);

  const handlePick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (!files.length) return;
    const names = files.map((f) => f.name);
    const hasModel = names.some((n) => n === "model.int8.onnx" || n === "model.onnx");
    if (!hasModel || !names.includes("tokens.txt")) {
      showToast("所选目录缺少 model.int8.onnx（或 model.onnx）与 tokens.txt", false);
      return;
    }
    const rel = (files[0] as File & { webkitRelativePath?: string }).webkitRelativePath || "";
    const name = rel.split("/")[0];
    try {
      // 点「添加模型」＝把该目录写进 config.json 的 models.list（自动保存）
      const r = await api.addModel(name, names);
      if (!r.ok) {
        showToast(r.error || `未在本机找到 ${name} 的模型目录`, false);
        return;
      }
      if (r.path) await switchModel(r.path);
      showToast(r.added ? `已添加并切换模型：${name}` : `模型已在列表中：${name}`);
    } catch (err) {
      showToast(String(err), false);
    }
  };

  /** 下载 SenseVoice int8（默认镜像）。每个模型卡片的下载按钮都走这里。 */
  const handleDownload = async () => {
    if (downloading) return;
    stopPolling();
    setDownloadJobId(null);
    setDownloadPct(null);
    setDownloading(true);
    try {
      const r = await api.startModelDownload("hf-mirror");
      if (!r.job_id) {
        setDownloading(false);
        showToast(`下载启动失败：${r.error || "未知错误"}`, false);
        return;
      }
      jobRef.current = r.job_id;
      setDownloadJobId(r.job_id);
      pollRef.current = setInterval(async () => {
        if (!jobRef.current) { stopPolling(); return; }
        try {
          const s = await api.getModelDownloadStatus(jobRef.current);
          setDownloadPct(s.progress);
          if (s.status === "completed") {
            stopPolling();
            setDownloading(false);
            setDownloadJobId(null);
            jobRef.current = null;
            showToast(`下载完成：${s.result?.path} (${fmtSize(s.result?.size || 0)})`);
            refresh();
          } else if (s.status === "failed") {
            stopPolling();
            setDownloading(false);
            setDownloadJobId(null);
            jobRef.current = null;
            showToast(`下载失败：${s.error || "未知错误"}`, false);
          } else if (s.status === "cancelled") {
            stopPolling();
            setDownloading(false);
            setDownloadJobId(null);
            jobRef.current = null;
          }
        } catch {
          // 忽略轮询错误，继续重试
        }
      }, 1000);
    } catch (err) {
      setDownloading(false);
      showToast(String(err), false);
    }
  };

  const handleCancelDownload = () => {
    if (!jobRef.current) return;
    api.cancelModelDownload(jobRef.current).catch(() => {});
    stopPolling();
    setDownloading(false);
    setDownloadJobId(null);
    jobRef.current = null;
  };

  const switchModel = async (path: string) => {
    try {
      await api.useModel(path);
      refresh();
      showToast("已切换模型 · 下次说话生效");
    } catch (e) {
      showToast(String(e), false);
    }
  };

  const modelOptionLabel = (m: ModelInfo) => `${m.name}（${m.quant} · ${fmtSize(m.size)}）`;

  return (
    <div className="p-6 mx-auto w-full max-w-4xl">
      <PageHeader title="模型" desc="更换模型后下次说话生效">
        <input
          ref={fileInput}
          type="file"
          multiple
          className="hidden"
          onChange={handlePick}
          // @ts-expect-error 非标准属性：让文件选择器选目录
          webkitdirectory="true"
          disabled={downloading}
        />
        <Button size="sm" variant="default" onClick={() => fileInput.current?.click()} disabled={downloading}>
          <Plus /> 添加模型
        </Button>
      </PageHeader>

      {/* 当前使用（紧凑） */}
      <div className="mb-1 mt-1 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
        当前使用
      </div>
      <Card className="mt-2 px-4 py-3">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <span className="w-28 shrink-0 text-xs text-muted-foreground">语音识别模型</span>
            <Select
              value={asrModels.find((m) => m.current)?.path ?? ""}
              onValueChange={switchModel}
            >
              <SelectTrigger className="flex-1">
                <SelectValue placeholder="选择语音识别模型" />
              </SelectTrigger>
              <SelectContent>
                {asrModels.map((m) => (
                  <SelectItem key={m.path} value={m.path}>{modelOptionLabel(m)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center gap-3">
            <span className="w-28 shrink-0 text-xs text-muted-foreground">文本润色模型</span>
            <Select
              value={llmModels.find((m) => m.current)?.path ?? ""}
              onValueChange={switchModel}
            >
              <SelectTrigger className="flex-1">
                <SelectValue placeholder="选择文本润色模型（可选）" />
              </SelectTrigger>
              <SelectContent>
                {llmModels.map((m) => (
                  <SelectItem key={m.path} value={m.path}>{modelOptionLabel(m)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
      </Card>

      {/* 全部模型 */}
      <div className="mt-5 mb-1 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
        模型列表
      </div>
      {models.list.length === 0 ? (
        <Card className="p-5">
          <div className="text-sm text-muted-foreground">未找到可用模型</div>
        </Card>
      ) : (
        <div className="flex flex-col gap-2">
          {models.list.map((m) => (
            <Card key={m.path} className="flex flex-row items-center gap-4 px-4 py-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold">{m.name}</span>
                  <Tag>{modelKind(m)}</Tag>
                  {m.current && <Tag>使用中</Tag>}
                </div>
                <div className="mt-1 flex items-center gap-2 truncate text-xs text-muted-foreground/70">
                  <span>{modelDesc(m)}</span>
                  <Tag>{m.quant}</Tag>
                  <span>{fmtSize(m.size)}</span>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {/* 下载 icon 按钮 */}
                {downloadJobId ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={handleCancelDownload}
                    title="取消下载"
                  >
                    {downloadPct != null ? `${downloadPct}%` : <Loader2 className="animate-spin size-4" />}
                    <X className="ml-1 size-3.5" />
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={handleDownload}
                    disabled={downloading}
                    title="下载模型"
                  >
                    {downloading ? <Loader2 className="animate-spin size-4" /> : <Download className="size-4" />}
                  </Button>
                )}
                {!m.current && (
                  <Button size="sm" variant="default" onClick={() => switchModel(m.path)} disabled={downloading}>
                    设为当前
                  </Button>
                )}
                <Button size="sm" variant="outline" title="打开目录" onClick={() => api.open(m.path)} disabled={downloading}>
                  <FolderOpen /> 
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* 下载完成/失败提示条（出错时显示，避免无反馈） */}
      {downloadJobId && downloadPct != null && (
        <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="animate-spin size-3.5" /> 下载中 {downloadPct}%
        </div>
      )}
    </div>
  );
}
