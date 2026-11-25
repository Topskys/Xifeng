import { useCallback, useEffect, useState } from "react";

import { api, type DailyPoint, type StatsPayload } from "@/api";
import { PageHeader } from "@/components/page";
import { HistorySection } from "@/pages/History";
import { Card } from "@/components/ui/card";
import type { ToastFn } from "@/lib/page-props";

const PERIODS = [
  { label: "今天", value: "today" },
  { label: "本周", value: "week" },
  { label: "全部", value: "all" },
];

const STAGE_COLORS: Record<string, string> = {
  asr: "bg-amber-500",
  post: "bg-emerald-500",
  inject: "bg-pink-500",
  other: "bg-blue-500",
};

function fmtMs(ms: number) {
  return ms > 0 ? (ms / 1000).toFixed(2) : "—";
}

export function StatsPage({
  showToast,
}: {
  showToast: ToastFn;
}) {
  const [period, setPeriod] = useState("today");
  const [data, setData] = useState<StatsPayload | null>(null);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => {
    api
      .stats(period)
      .then(setData)
      .catch(() => {});
  }, [period]);

  useEffect(refresh, [refresh, tick]);
  useEffect(() => {
    const t = window.setInterval(() => setTick((n) => n + 1), 5000);
    return () => window.clearInterval(t);
  }, []);

  if (!data) return <div className="p-6" />;

  const { kpis, rows } = data;
  const maxMs = Math.max(2000, ...rows.map((r) => r.e2e_ms));

  return (
    <div className="p-6">
      <PageHeader title="统计" desc="每 5 秒自动刷新">
        <div className="flex rounded-md border border-border p-0.5">
          {PERIODS.map((p) => (
            <button
              key={p.value}
              onClick={() => setPeriod(p.value)}
              className={
                "cursor-pointer rounded-sm px-3 py-1 text-xs transition-colors " +
                (period === p.value
                  ? "bg-secondary font-medium text-secondary-foreground"
                  : "text-muted-foreground hover:text-foreground")
              }
            >
              {p.label}
            </button>
          ))}
        </div>
      </PageHeader>

      {/* KPI */}
      <div className="grid grid-cols-5 gap-3">
        {(
          [
            [
              "端到端延迟 · P50",
              fmtMs(kpis.p50),
              kpis.p50 > 0 ? "s" : "",
              `基于 ${kpis.total} 次`,
            ],
            [
              "端到端延迟 · P95",
              fmtMs(kpis.p95),
              kpis.p95 > 0 ? "s" : "",
              kpis.p95 > 0
                ? kpis.p95 <= 1500
                  ? "✓ 目标 <1.5s"
                  : "超出 1.5s 目标"
                : "暂无数据",
            ],
            [
              "输入字数",
              String(kpis.charsToday),
              "",
              `今日 · 累计 ${kpis.charsAll} 字`,
            ],
            [
              "识别次数",
              String(kpis.total),
              "",
              kpis.total > 0
                ? `成功率 ${(kpis.successRate * 100).toFixed(0)}% · 失败 ${kpis.failed} · 丢弃 ${kpis.dropped}`
                : "按住 Ctrl 说一句话试试",
            ],
            ["超时次数", String(kpis.timeouts), "", "e2e > 5s 视为超时"],
          ] as const
        ).map(([label, value, unit, hint], i) => (
          <Card key={i} className="p-4">
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className="mt-1.5 flex items-baseline gap-1">
              <span className="text-2xl font-bold tracking-tight">{value}</span>
              {unit && (
                <span className="text-xs text-muted-foreground">{unit}</span>
              )}
            </div>
            <div
              className={
                "mt-1 text-xs " +
                (hint.startsWith("✓")
                  ? "text-success"
                  : hint.startsWith("超出")
                    ? "text-destructive"
                    : "text-muted-foreground")
              }
            >
              {hint}
            </div>
          </Card>
        ))}
      </div>

      {/* 延迟分析（瀑布） */}
      <Card className="p-5 mt-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold uppercase tracking-wide text-muted-foreground/70">
            延迟分析
          </span>
          <span className="text-xs text-muted-foreground">
            最近 {rows.length} 次
          </span>
        </div>
        {rows.length === 0 ? (
          <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
            暂无耗时数据 —— 按住 Ctrl 说一句话
          </div>
        ) : (
          <>
            <div className="mt-4 flex flex-col gap-1.5">
              {rows.map((r, i) => (
                <div key={i} className="flex items-center gap-3">
                  <div className="relative h-2.5 flex-1 overflow-hidden rounded-sm bg-muted">
                    <div className="flex h-full">
                      {(
                        [
                          [
                            "other",
                            Math.max(
                              0,
                              r.e2e_ms - r.ms_asr - r.ms_post - r.ms_inject,
                            ),
                          ],
                          ["asr", r.ms_asr],
                          ["post", r.ms_post],
                          ["inject", r.ms_inject],
                        ] as const
                      ).map(([k, ms]) =>
                        ms > 0 ? (
                          <div
                            key={k}
                            className={STAGE_COLORS[k]}
                            style={{ width: `${(ms / maxMs) * 100}%` }}
                          />
                        ) : null,
                      )}
                    </div>
                    <div
                      className="absolute top-0 h-full w-px bg-destructive/70"
                      style={{
                        left: `${Math.min(100, (1500 / maxMs) * 100)}%`,
                      }}
                    />
                  </div>
                  <span className="w-16 text-right text-xs tabular-nums text-muted-foreground">
                    {r.e2e_ms} ms
                  </span>
                </div>
              ))}
            </div>
            <div className="mt-3 flex items-center gap-4 border-t border-border pt-3 text-xs text-muted-foreground">
              {(
                [
                  ["开销", "other"],
                  ["ASR", "asr"],
                  ["清理", "post"],
                  ["注入", "inject"],
                ] as const
              ).map(([label, k]) => (
                <span key={k} className="flex items-center gap-1.5">
                  <span className={`size-2 rounded-sm ${STAGE_COLORS[k]}`} />
                  {label}
                </span>
              ))}
              <span className="ml-auto flex items-center gap-1.5">
                <span className="h-3 w-px bg-destructive/70" />
                1.5s 目标线
              </span>
            </div>
          </>
        )}
      </Card>

      {/* 历史趋势 */}
      <Card className="p-5 mt-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold uppercase tracking-wide text-muted-foreground/70">
            历史趋势
          </span>
          <div className="flex items-center gap-4 text-xs text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <span className="size-2 rounded-sm bg-blue-500" />
              输入字数（左轴）
            </span>
            <span className="flex items-center gap-1.5">
              <span className="size-2 rounded-sm bg-blue-300" />
              识别次数（左轴）
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-0.5 w-4 rounded bg-amber-500" />
              平均端到端延迟（右轴）
            </span>
          </div>
        </div>
        <TrendChart daily={data.daily} />
      </Card>

      {/* 历史记录 */}
      <div className="mt-6 mb-2 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
      <HistorySection showToast={showToast} period={period} />
      </div>
    </div>
  );
}

/** Catmull-Rom → 三次贝塞尔：折线平滑成曲线 */
function smoothLine(pts: { x: number; y: number }[]) {
  if (pts.length < 2) return ""
  let d = `M ${pts[0].x},${pts[0].y}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)]
    const p1 = pts[i]
    const p2 = pts[i + 1]
    const p3 = pts[Math.min(pts.length - 1, i + 2)]
    const c1x = p1.x + (p2.x - p0.x) / 6
    const c1y = p1.y + (p2.y - p0.y) / 6
    const c2x = p2.x - (p3.x - p1.x) / 6
    const c2y = p2.y - (p3.y - p1.y) / 6
    d += ` C ${c1x},${c1y} ${c2x},${c2y} ${p2.x},${p2.y}`
  }
  return d
}

function TrendChart({ daily }: { daily: DailyPoint[] }) {
  const W = 1000;
  const H = 260;
  const L = 48;
  const R = 60;
  const T = 14;
  const B = 30;
  const n = Math.max(1, daily.length);
  const iw = W - L - R;
  const ih = H - T - B;
  const maxChars = Math.max(1, ...daily.map((d) => d.chars));
  const maxCount = Math.max(1, ...daily.map((d) => d.count));
  const maxE = Math.max(1000, ...daily.map((d) => d.avg_e2e));
  const slot = iw / n;
  const bw = Math.max(3, Math.min(18, slot * 0.56));
  const x = (i: number) => L + slot * (i + 0.5);
  const yC = (v: number) => T + ih - (v / maxChars) * ih;
  const yN = (v: number) => T + ih - (v / maxCount) * ih;
  const yE = (v: number) => T + ih - (v / maxE) * ih;

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img">
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <line
            key={f}
            x1={L}
            x2={W - R}
            y1={T + ih * (1 - f)}
            y2={T + ih * (1 - f)}
            stroke="currentColor"
            className="text-border"
            strokeDasharray="3 4"
          />
        ))}
        <line
          x1={L}
          x2={W - R}
          y1={T + ih}
          y2={T + ih}
          stroke="currentColor"
          className="text-border"
        />
        {daily.map((d, i) => (
          <g key={d.date}>
            <rect
              x={x(i) - bw + 1}
              y={yC(d.chars)}
              width={bw / 2 - 1}
              height={Math.max(1, T + ih - yC(d.chars))}
              rx={2}
              className="fill-blue-500"
            >
              <title>{`${d.date} 字数 ${d.chars}`}</title>
            </rect>
            <rect
              x={x(i) + 1}
              y={yN(d.count)}
              width={bw / 2 - 1}
              height={Math.max(1, T + ih - yN(d.count))}
              rx={2}
              className="fill-blue-300"
            >
              <title>{`${d.date} 次数 ${d.count}`}</title>
            </rect>
            <text
              x={x(i)}
              y={H - 8}
              textAnchor="middle"
              className="fill-muted-foreground"
              fontSize={10}
            >
              {d.date.slice(5)}
            </text>
          </g>
        ))}
        <path d={smoothLine(daily.map((d, i) => ({ x: x(i), y: yE(d.avg_e2e) })))} fill="none" stroke="#f59e0b" strokeWidth={2} />
        {daily.map((d, i) => (
          <circle key={i} cx={x(i)} cy={yE(d.avg_e2e)} r={3} fill="#f59e0b">
            <title>{`${d.date} 平均延迟 ${d.avg_e2e}ms`}</title>
          </circle>
        ))}
        <text
          x={L - 6}
          y={yC(maxChars) + 4}
          textAnchor="end"
          className="fill-muted-foreground"
          fontSize={10}
        >
          {maxChars}
        </text>
        <text
          x={L - 6}
          y={T + ih + 4}
          textAnchor="end"
          className="fill-muted-foreground"
          fontSize={10}
        >
          0
        </text>
        <text
          x={W - R + 8}
          y={yE(maxE) + 4}
          className="fill-muted-foreground"
          fontSize={10}
        >
          {maxE}ms
        </text>
        <text
          x={W - R + 8}
          y={T + ih + 4}
          className="fill-muted-foreground"
          fontSize={10}
        >
          0
        </text>
      </svg>
    </div>
  );
}
