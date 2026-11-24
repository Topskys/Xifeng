import { FolderOpen, Plus, RefreshCw, Download, AlertCircle, Loader2 } from "lucide-react";
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

/** 按目录名推断模型用途（截图里的「语音识别 / 文本润色」分类，纯文字用）。 */
function modelKind(m: ModelInfo): string {
  const n = m.name.toLowerCase();
  if (n.includes("sensevoice") || n.includes("paraformer") || n.includes("firered") || n.includes("whisper")) {
    return "语音识别";
  }
  if (n.includes("qwen") || n.includes("llm") || n.includes("0.5b") || n.includes("1.5b")) {
    return "文本润色";
  }
  return "模型";
}

/** 模型描述（截图里那行小字，纯文字用）。 */
function modelDesc(m: ModelInfo): string {
  const n = m.name.toLowerCase();
  if (n.includes("sensevoice")) return "中英日粤韩 · 精度高速度快";
  if (n.includes("paraformer")) return "轻量流式识别";
  if (n.includes("firered")) return "更高精度中文识别";
  if (n.includes("0.5b")) return "智能标点与口语转书面语";
  if (n.includes("1.5b")) return "更强的润色与纠错能力";
  return "本地模型";
}

const MIRRORS = [
  { value: "hf-mirror", label: "HuggingFace 镜像 (hf-mirror.com)" },
  { value: "modelscope", label: "ModelScope (modelscope.cn)" },
  { value: "github", label: "GitHub Release (huggingface.co)" },
] as const;

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
  const asrModels = models.list.filter((m) => modelKind(m) === "语音识别");
  const llmModels = models.list.filter((m) => modelKind(m) === "文本润色");

  const [downloading, setDownloading] = useState(false);
  const [selectedMirror, setSelectedMirror] = useState<string>(MIRRORS[0].value);
  const [downloadJobId, setDownloadJobId] = useState<string | null>(null);
  const [downloadPct, setDownloadPct] = useState<number | null>(null);
  const [downloadStatus, setDownloadStatus] = useState<string>("");
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
      const r = await api.modelsFind(name, names);
      if (r.matches?.length) {
        await switchModel(r.matches[0]);
        showToast(`已添加并切换模型：${name}`);
      } else {
        showToast(`未在本机找到 ${name} 的模型目录（已搜索模型目录与用户目录）`, false);
      }
    } catch (err) {
      showToast(String(err), false);
    }
  };

  const handleDownload = async () => {
    stopPolling();
    setDownloadJobId(null);
    setDownloadPct(null);
    setDownloadStatus("queued");
    setDownloading(true);
    jobRef.current = null;
    try {
      const r = await api.startModelDownload(selectedMirror);
      if (!r.job_id) {
        setDownloadStatus("error");
        setDownloading(false);
        showToast(`下载启动失败：${r.error || "未知错误"}`);
        return;
      }
      setDownloadJobId(r.job_id);
      jobRef.current = r.job_id;
      pollRef.current = setInterval(async () => {
        if (!jobRef.current) { stopPolling(); setDownloading(false); return; }
        try {
          const s = await api.getModelDownloadStatus(jobRef.current);
          setDownloadPct(s.progress);
          setDownloadStatus(s.status);
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
            showToast(`下载失败：${s.error || "未知错误"}`);
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
      setDownloadStatus("error");
      setDownloading(false);
      showToast(String(err));
    }
  };

  const handleCancelDownload = () => {
    if (!jobRef.current) return;
    api.cancelModelDownload(jobRef.current).catch(() => {});
    stopPolling();
    setDownloading(false);
    setDownloadStatus("cancelled");
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

  /** 当前使用区下拉：显示「模型名（量化 · 大小）」，纯文字。 */
  const modelOptionLabel = (m: ModelInfo) => `${m.name}（${m.quant} · ${fmtSize(m.size)}）`;

  return (
    <div className="p-6 mx-auto w-full max-w-4xl">
      <PageHeader title="模型" desc="更换模型后下次说话生效">
        <Button size="sm" variant="ghost" onClick={refresh} disabled={downloading}>
          <RefreshCw className={downloading ? "animate-spin" : ""} /> 刷新
        </Button>
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

      {/* 当前使用 */}
      <div className="mb-2 mt-1 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
        当前使用
      </div>
      <Card className="mt-4 p-5">
        <div className="mb-3 text-sm text-muted-foreground">语音识别模型</div>
        <Select
          value={asrModels.find((m) => m.current)?.path ?? ""}
          onValueChange={switchModel}
        >
          <SelectTrigger className="w-full">
            <SelectValue placeholder="选择语音识别模型" />
          </SelectTrigger>
          <SelectContent>
            {asrModels.map((m) => (
              <SelectItem key={m.path} value={m.path}>{modelOptionLabel(m)}</SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="mb-3 mt-4 text-sm text-muted-foreground">文本润色模型（可选）</div>
        <Select
          value={llmModels.find((m) => m.current)?.path ?? ""}
          onValueChange={switchModel}
        >
          <SelectTrigger className="w-full">
            <SelectValue placeholder="选择文本润色模型" />
          </SelectTrigger>
          <SelectContent>
            {llmModels.map((m) => (
              <SelectItem key={m.path} value={m.path}>{modelOptionLabel(m)}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Card>

      {/* 全部模型 */}
      <div className="mt-6 mb-2 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
        全部模型
      </div>
      {models.list.length === 0 ? (
        <Card className="p-5">
          <div className="text-sm text-muted-foreground">未找到可用模型</div>
        </Card>
      ) : (
        <div className="flex flex-col gap-3">
          {models.list.map((m) => (
            <Card key={m.path} className="flex flex-row items-center gap-4 p-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-3">
                  <span className="text-sm font-semibold">{m.name}</span>
                  <span className="text-xs text-muted-foreground">{modelKind(m)}</span>
                  <span className="text-xs text-muted-foreground">{m.quant}</span>
                  {m.current && <span className="text-xs text-muted-foreground">使用中</span>}
                </div>
                <div className="mt-1 truncate text-xs text-muted-foreground/70">
                  {modelDesc(m)} · {fmtSize(m.size)}
                </div>
              </div>
              <div className="flex shrink-0 gap-2">
                {!m.current && (
                  <Button
                    size="sm"
                    variant="default"
                    onClick={() => switchModel(m.path)}
                    disabled={downloading}
                  >
                    设为当前
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => api.open(m.path)}
                  disabled={downloading}
                >
                  <FolderOpen /> 打开目录
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* 下载模型 */}
      <div className="mt-6 mb-2 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
        下载模型
      </div>
      <Card className="mt-4 p-5">
        <div className="text-sm font-semibold mb-3">下载 SenseVoice int8 模型</div>
        <p className="text-xs text-muted-foreground mb-4">
          约 228 MB，下载后自动校验大小与文件完整性。支持断点续传。
        </p>

        <div className="flex flex-col sm:flex-row gap-3 mb-4">
          <Select value={selectedMirror} onValueChange={setSelectedMirror}>
            <SelectTrigger className="w-full sm:w-[200px]">
              <SelectValue placeholder="选择下载源" />
            </SelectTrigger>
            <SelectContent>
              {MIRRORS.map((m) => (
                <SelectItem key={m.value} value={m.value}>
                  {m.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Button
            size="default"
            variant={downloadStatus === "failed" || downloadStatus === "cancelled" ? "outline" : "default"}
            onClick={downloadJobId ? handleCancelDownload : handleDownload}
            disabled={downloadStatus === "completed"}
            className="w-full sm:w-auto flex-1"
          >
            {downloadJobId ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {downloadStatus === "cancelled" ? "取消中…" : "取消下载"}
              </>
            ) : downloadStatus === "completed" ? (
              <>
                <Download className="mr-2 h-4 w-4" />
                已下载
              </>
            ) : (
              <>
                <Download className="mr-2 h-4 w-4" />
                开始下载
              </>
            )}
          </Button>
        </div>

        {downloadJobId && (
          <div className="mb-3">
            <div className="flex justify-between text-xs mb-1">
              <span className="text-muted-foreground">{downloadStatus}</span>
              <span className="text-muted-foreground">{downloadPct != null ? downloadPct + "%" : ""}</span>
            </div>
            <div className="w-full bg-secondary rounded-full h-2">
              <div
                className="bg-primary h-2 rounded-full transition-all duration-300"
                style={{ width: downloadPct != null ? Math.max(0, Math.min(100, downloadPct)) : 0 }}
              />
            </div>
          </div>
        )}

        {downloadStatus === "failed" && (
          <div className="mb-3 p-3 text-xs bg-muted/50 rounded-md border border-border flex items-start gap-2">
            <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
            <span>下载失败，可重试。</span>
          </div>
        )}
      </Card>
    </div>
  );
}
