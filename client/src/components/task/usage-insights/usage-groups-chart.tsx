/**
 * Grouped usage breakdown chart.
 *
 * Responsibility: render a single-dimension bar chart for the insights groups
 * endpoint. `bucket_key` drives the X axis verbatim (including the literal
 * "unknown" bucket), `cost_usd` drives the primary Y axis. All numbers come
 * from the backend response as-is; this component only formats for display.
 * Wraps the shared `ChartContainer` primitive rather than importing Recharts
 * layout directly at the page level (see ownership checklist:
 * reuse-chart-primitives, no-frontend-cost-math, explicit-unknown-buckets).
 */

import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";

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

import { useUsageInsightsGroups } from "@/hooks/useUsageInsights";
import {
  formatCostUsd,
  type GroupByKey,
  type UsageInsightsFilters,
} from "@/types/usage";

export interface UsageGroupsChartProps {
  taskId: number | null | undefined;
  groupBy: GroupByKey;
  filters: UsageInsightsFilters;
}

const CHART_CONFIG: ChartConfig = {
  cost_usd: {
    label: "Cost (USD)",
    color: "hsl(217 91% 60%)",
  },
};

const GROUP_BY_LABELS: Record<GroupByKey, string> = {
  role: "Role",
  node_name: "Node",
  execution_branch: "Branch",
  provider: "Provider",
  model: "Model",
  api_surface: "API surface",
};

function ChartFrame(props: {
  groupBy: GroupByKey;
  children: React.ReactNode;
}) {
  return (
    <Card data-testid="usage-groups-chart">
      <CardHeader>
        <CardTitle className="text-base font-medium">
          Breakdown by {GROUP_BY_LABELS[props.groupBy].toLowerCase()}
        </CardTitle>
        <CardDescription>
          Cost grouped by {GROUP_BY_LABELS[props.groupBy].toLowerCase()}. The
          literal bucket &quot;unknown&quot; represents rows with missing
          metadata.
        </CardDescription>
      </CardHeader>
      <CardContent>{props.children}</CardContent>
    </Card>
  );
}

export function UsageGroupsChart({
  taskId,
  groupBy,
  filters,
}: UsageGroupsChartProps) {
  const { data, isLoading, isError, error } = useUsageInsightsGroups(
    taskId,
    groupBy,
    filters,
  );

  if (taskId == null) {
    return (
      <ChartFrame groupBy={groupBy}>
        <div className="text-sm text-muted-foreground">
          Select a task to see grouped usage.
        </div>
      </ChartFrame>
    );
  }

  if (isLoading) {
    return (
      <ChartFrame groupBy={groupBy}>
        <Skeleton className="h-48 w-full" />
      </ChartFrame>
    );
  }

  if (isError || !data) {
    return (
      <ChartFrame groupBy={groupBy}>
        <div className="text-sm text-destructive">
          Failed to load grouped usage
          {error instanceof Error ? `: ${error.message}` : ""}.
        </div>
      </ChartFrame>
    );
  }

  if (data.items.length === 0) {
    return (
      <ChartFrame groupBy={groupBy}>
        <div className="text-sm text-muted-foreground">
          No rows to group for the current filters.
        </div>
      </ChartFrame>
    );
  }

  // Recharts wants a plain array. We pass rows straight through so the
  // backend-authored numbers (including the explicit-unknown bucket) survive.
  const rows = data.items;

  return (
    <ChartFrame groupBy={groupBy}>
      <ChartContainer config={CHART_CONFIG} className="h-64 w-full">
        <BarChart
          accessibilityLayer
          data={rows}
          margin={{ top: 8, right: 12, left: 12, bottom: 8 }}
        >
          <CartesianGrid vertical={false} />
          <XAxis
            dataKey="bucket_key"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            interval={0}
            angle={rows.length > 6 ? -25 : 0}
            height={rows.length > 6 ? 48 : 24}
            textAnchor={rows.length > 6 ? "end" : "middle"}
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
                formatter={(value) =>
                  typeof value === "number"
                    ? formatCostUsd(value)
                    : String(value)
                }
                hideIndicator={false}
              />
            }
          />
          <Bar
            dataKey="cost_usd"
            fill="var(--color-cost_usd)"
            radius={[4, 4, 0, 0]}
          />
        </BarChart>
      </ChartContainer>
    </ChartFrame>
  );
}

export default UsageGroupsChart;
