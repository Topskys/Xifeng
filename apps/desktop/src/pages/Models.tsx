import { FolderOpen, Plus, RefreshCw } from "lucide-react";
import { useRef } from "react";

import { api, type Bootstrap } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/pages/Stats";

function fmtSize(n: number) {
  if (n <= 0) return "0 MB";
  return n >= 1024 ** 3
    ? `${(n / 1024 ** 3).toFixed(1)} GB`
    : `${(n / 1024 ** 2).toFixed(0)} MB`;
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
        await use(r.matches[0]);
        showToast(`已添加并切换模型：${name}`);
      } else {
        showToast(`未在本机找到 ${name} 的模型目录（已搜索模型目录与用户目录）`, false);
      }
    } catch (err) {
      showToast(String(err), false);
    }
  };

  const use = async (path: string) => {
    try {
      await api.useModel(path);
      refresh();
      showToast("已切换模型 · 托盘重启后生效");
    } catch (e) {
      showToast(String(e), false);
    }
  };

  return (
    <div className="p-6 mx-auto w-full max-w-4xl">
      <PageHeader title="模型" desc="更换模型后建议重启">
        <Button size="sm" variant="ghost" onClick={refresh}>
          <RefreshCw /> 刷新
        </Button>
        <input
          ref={fileInput}
          type="file"
          multiple
          className="hidden"
          onChange={handlePick}
          // @ts-expect-error 非标准属性：让文件选择器选目录
          webkitdirectory="true"
        />
        <Button size="sm" variant="default" onClick={() => fileInput.current?.click()}>
          <Plus /> 添加模型
        </Button>
      </PageHeader>

      <div className="mb-2 mt-1 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
        可用模型列表
      </div>
      {models.list.length === 0 ? (
        <Card className="p-5">
          <div className="text-sm font-semibold text-destructive">
            未找到可用模型
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            把 sensevoice-small-int8 目录放到模型路径，或在上方添加。需要
            model.int8.onnx（或 model.onnx）与 tokens.txt。
          </p>
        </Card>
      ) : (
        <div className="flex flex-col gap-3">
          {models.list.map((m) => (
            <Card key={m.path} className="flex flex-row items-center gap-4 p-4">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-secondary text-sm font-bold">
                SV
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold">{m.name}</span>
                  <Badge variant="muted">{m.quant}</Badge>
                  <Badge variant="muted">{fmtSize(m.size)}</Badge>
                  {m.current && <Badge variant="success">当前使用</Badge>}
                </div>
                <div className="mt-1 truncate text-xs text-muted-foreground/70">
                  {m.ready ? m.path : "缺少模型文件"}
                </div>
              </div>
              <div className="flex shrink-0 gap-2">
                {!m.current && (
                  <Button
                    size="sm"
                    variant="default"
                    onClick={() => use(m.path)}
                  >
                    设为当前
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => api.open(m.path)}
                >
                  <FolderOpen /> 打开目录
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      <div className="mt-6 mb-2 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
        说明
      </div>
      <Card className="mt-4 p-5">
        <div className="text-sm font-semibold">模型目录</div>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          本版本只支持 SenseVoice 系列（sherpa-onnx）。目录内需要
          model.int8.onnx（或 model.onnx）与 tokens.txt；换模型后托盘重启生效。
        </p>
      </Card>
    </div>
  );
}
