/**
 * Usage timeline chart.
 *
 * Responsibility: render the task's chronological per-call usage as a single
 * line chart of `cumulative_cost_usd` against `created_at`. Points come from
 * the backend /usage/insights/timeline response verbatim; the component only
 * formats tooltip values and tick labels (see ownership checklist:
 * reuse-chart-primitives, simple-timeline-shape, no-frontend-cost-math).
 */

import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";

import { useUsageInsightsTimeline } from "@/hooks/useUsageInsights";
import {
  formatCostUsd,
  type UsageInsightsFilters,
} from "@/types/usage";

export interface UsageTimelineChartProps {
  taskId: number | null | undefined;
  filters: UsageInsightsFilters;
}

const CHART_CONFIG: ChartConfig = {
  cumulative_cost_usd: {
    label: "Cumulative cost",
    color: "hsl(142 71% 45%)",
  },
};

/** Format an ISO timestamp for a compact X-axis tick. Empty strings (legacy
 *  "null upstream" convention from the timeline schema) fall back to "—". */
function formatTimeTick(iso: string): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function TimelineFrame(props: { children: React.ReactNode }) {
  return (
    <Card data-testid="usage-timeline-chart">
      <CardHeader>
        <CardTitle className="text-base font-medium">Timeline</CardTitle>
        <CardDescription>
          Cumulative cost across all LLM calls for this task in chronological
          order.
        </CardDescription>
      </CardHeader>
      <CardContent>{props.children}</CardContent>
    </Card>
  );
}

export function UsageTimelineChart({
  taskId,
  filters,
}: UsageTimelineChartProps) {
  const { data, isLoading, isError, error } = useUsageInsightsTimeline(
    taskId,
    filters,
  );

  if (taskId == null) {
    return (
      <TimelineFrame>
        <div className="text-sm text-muted-foreground">
          Select a task to see the timeline.
        </div>
      </TimelineFrame>
    );
  }

  if (isLoading) {
    return (
      <TimelineFrame>
        <Skeleton className="h-48 w-full" />
      </TimelineFrame>
    );
  }

  if (isError || !data) {
    return (
      <TimelineFrame>
        <div className="text-sm text-destructive">
          Failed to load timeline
          {error instanceof Error ? `: ${error.message}` : ""}.
        </div>
      </TimelineFrame>
    );
  }

  if (data.items.length === 0) {
    return (
      <TimelineFrame>
        <div className="text-sm text-muted-foreground">
          No LLM calls recorded in this window.
        </div>
      </TimelineFrame>
    );
  }

  const rows = data.items;

  return (
    <TimelineFrame>
      <ChartContainer config={CHART_CONFIG} className="h-64 w-full">
        <LineChart
          accessibilityLayer
          data={rows}
          margin={{ top: 8, right: 12, left: 12, bottom: 8 }}
        >
          <CartesianGrid vertical={false} />
          <XAxis
            dataKey="created_at"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            tickFormatter={formatTimeTick}
            minTickGap={32}
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            tickFormatter={(value: number) => formatCostUsd(value)}
            width={72}
          />
          <ChartTooltip
            cursor={false}
            content={
              <ChartTooltipContent
                labelFormatter={(label) =>
                  typeof label === "string" ? formatTimeTick(label) : String(label)
                }
                formatter={(value) =>
                  typeof value === "number"
                    ? formatCostUsd(value)
                    : String(value)
                }
                hideIndicator={false}
              />
            }
          />
          <Line
            type="monotone"
            dataKey="cumulative_cost_usd"
            stroke="var(--color-cumulative_cost_usd)"
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ChartContainer>
    </TimelineFrame>
  );
}

export default UsageTimelineChart;
