import { Copy, Search, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, type HistoryRow } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const PAGE_SIZE = 50;

function fmtTime(ts: string) {
  const d = new Date(ts);
  return isNaN(+d)
    ? ts.slice(0, 19).replace("T", " ")
    : d.toLocaleTimeString("zh-CN", { hour12: false });
}

export function HistorySection({
  showToast,
  period,
}: {
  showToast: (t: string, ok?: boolean) => void;
  period: string;
}) {
  const [rows, setRows] = useState<HistoryRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  const load = useCallback(
    async (off: number, replace: boolean) => {
      setLoading(true);
      try {
        const r = await api.history(period, off, PAGE_SIZE);
        setRows((prev) => (replace ? r.rows : [...prev, ...r.rows]));
        setHasMore(r.has_more);
        setOffset(off);
        setTotal(r.rows.length);
      } catch {
        /* 服务不可用时保持现状 */
      } finally {
        setLoading(false);
      }
    },
    [period],
  );

  // 周期切换 → 重置分页
  useEffect(() => {
    void load(0, true);
  }, [load]);

  const filtered = rows.filter((r) => {
    if (!r.text && !r.clean_text && !r.raw_text) return false; // 无效输入不展示
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return [r.raw_text, r.clean_text, r.text].some(
      (t) => t && t.toLowerCase().includes(q),
    );
  });

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el || loading || !hasMore) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 60) {
      void load(offset + PAGE_SIZE, false);
    }
  };

  const del = async (row: HistoryRow) => {
    if (!window.confirm("删除这条历史记录？（不影响识别与术语表）")) return;
    await api.deleteHistory(row.id);
    void load(0, true);
    showToast("已删除");
  };

  return (
    <div className="w-full">
      {/* 标题 + 搜索 左右布局 */}
      <div className="mb-2 flex items-center justify-between px-1">
        <div className="text-sm font-semibold uppercase tracking-wide text-muted-foreground/70">
          历史输入{" "}
          {!loading && !hasMore && filtered.length > 0 && (
            <span>（近 {total} 条）</span>
          )}
        </div>

        <div className="relative w-64">
          <Search className="absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
          <Input
            className="h-8 pl-8 font-normal normal-case"
            placeholder="搜索文本…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </div>

      {/* 固定高度表格：滚动触底加载下一页 */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="h-80 overflow-y-auto rounded-xl border border-border bg-card"
      >
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-card">
            <TableRow>
              <TableHead className="w-20">时间</TableHead>
              <TableHead>文本</TableHead>
              <TableHead className="w-20 text-right">延迟</TableHead>
              <TableHead className="w-20 text-right">耗时</TableHead>
              <TableHead className="w-24 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="h-32 text-center text-sm text-muted-foreground"
                >
                  {query
                    ? "没有匹配的记录。"
                    : "还没有记录 —— 按住 Ctrl 说一句话。"}
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((r) => {
                const main = r.text || r.clean_text || r.raw_text || "";
                            return (
                  <TableRow
                    key={r.id}
                    className=""
                  >
                    <TableCell className="text-xs tabular-nums text-muted-foreground">
                      {fmtTime(r.ts)}
                    </TableCell>
                    <TableCell className="max-w-0">
                      <span
                        className="block truncate text-sm"
                        title={main}
                      >
                        {main}
                      </span>
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums text-muted-foreground">
                      {r.e2e_ms}ms
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums text-muted-foreground">
                      {(r.speech_ms / 1000).toFixed(1)}s
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        size="xs"
                        variant="ghost"
                        className="mr-1"
                        onClick={async () => {
                          const text =
                            r.text || r.clean_text || r.raw_text || "";
                          if (!text) {
                            showToast("这条记录没有保存文本", false);
                            return;
                          }
                          await navigator.clipboard.writeText(text);
                          showToast("已复制");
                        }}
                      >
                        <Copy />
                      </Button>
                      <Button
                        size="xs"
                        variant="ghost"
                        className="text-muted-foreground"
                        onClick={() => void del(r)}
                      >
                        <Trash2 />
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
        {loading && (
          <div className="py-2 text-center text-xs text-muted-foreground">
            加载中…
          </div>
        )}
        {loading && hasMore && <div className="h-10" />}
      </div>
    </div>
  );
}
