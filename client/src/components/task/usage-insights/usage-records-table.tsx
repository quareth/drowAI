/**
 * Usage records table.
 *
 * Responsibility: render a paginated list of per-call LLM usage rows for a
 * single task. Uses the shared `useUsageInsightsRecords` hook with local page
 * state; page size stays fixed at 25 rows. Cache-reporting status is surfaced
 * per row via `formatCacheReportingLabel` so "reported", "not_reported", and
 * "unknown" are visibly distinguished (see ownership checklist:
 * honest-cache-reporting, explicit-unknown-buckets, no-frontend-cost-math).
 */

import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

import { useUsageInsightsRecords } from "@/hooks/useUsageInsights";
import {
  formatCacheReportingLabel,
  formatPricedCostUsd,
  type CacheReporting,
  type UsageInsightsFilters,
  type UsageInsightsRecord,
} from "@/types/usage";

export interface UsageRecordsTableProps {
  taskId: number | null | undefined;
  filters: UsageInsightsFilters;
}

const PAGE_SIZE = 25;

/** Badge variant for the cache_reporting column. Kept local to this file
 *  since no other component in the panel surfaces this column. */
function cacheReportingBadgeVariant(
  value: CacheReporting,
): "default" | "secondary" | "outline" {
  switch (value) {
    case "reported":
      return "default";
    case "not_reported":
      return "secondary";
    case "unknown":
      return "outline";
  }
}

function formatCreatedAt(iso: string): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

function TableFrame(props: { children: React.ReactNode }) {
  return (
    <Card data-testid="usage-records-table">
      <CardHeader>
        <CardTitle className="text-base font-medium">Records</CardTitle>
        <CardDescription>
          Per-call usage rows with canonical metadata and honest cache-reporting
          status.
        </CardDescription>
      </CardHeader>
      <CardContent>{props.children}</CardContent>
    </Card>
  );
}

/** Column definition for the records table, kept co-located with the row
 *  render to keep the component surface small. */
interface Column {
  key: string;
  header: string;
  render: (record: UsageInsightsRecord) => React.ReactNode;
  /** Right-align numeric columns so values sit under the same decimal column. */
  numeric?: boolean;
}

const COLUMNS: ReadonlyArray<Column> = [
  {
    key: "created_at",
    header: "Created",
    render: (r) => (
      <span className="whitespace-nowrap text-xs text-muted-foreground">
        {formatCreatedAt(r.created_at)}
      </span>
    ),
  },
  { key: "model", header: "Model", render: (r) => r.model },
  { key: "role", header: "Role", render: (r) => r.role },
  { key: "node_name", header: "Node", render: (r) => r.node_name },
  {
    key: "execution_branch",
    header: "Branch",
    render: (r) => r.execution_branch,
  },
  { key: "provider", header: "Provider", render: (r) => r.provider },
  {
    key: "api_surface",
    header: "API Surface",
    render: (r) => r.api_surface,
  },
  {
    key: "tokens",
    header: "Tokens",
    numeric: true,
    render: (r) => (
      <span className="tabular-nums">{r.total_tokens.toLocaleString()}</span>
    ),
  },
  {
    key: "cached",
    header: "Cached",
    numeric: true,
    render: (r) => (
      <span className="tabular-nums">{r.cached_tokens.toLocaleString()}</span>
    ),
  },
  {
    key: "cost",
    header: "Cost",
    numeric: true,
    render: (r) => (
      <span className="tabular-nums">
        {formatPricedCostUsd(r.cost_usd, r.pricing_status)}
      </span>
    ),
  },
  {
    key: "cache_reporting",
    header: "Cache reporting",
    render: (r) => (
      <Badge
        variant={cacheReportingBadgeVariant(r.cache_reporting)}
        data-testid={`usage-record-cache-${r.id}`}
        className="text-xs font-normal"
      >
        {formatCacheReportingLabel(r.cache_reporting)}
      </Badge>
    ),
  },
];

export function UsageRecordsTable({
  taskId,
  filters,
}: UsageRecordsTableProps) {
  const [page, setPage] = useState(1);

  // Reset pagination whenever the caller swaps the task or the filter set so
  // we never show page=3 of the previous task/filter accidentally.
  const filtersFingerprint = useMemo(
    () => JSON.stringify(filters ?? {}),
    [filters],
  );
  useEffect(() => {
    setPage(1);
  }, [taskId, filtersFingerprint]);

  const { data, isLoading, isError, error } = useUsageInsightsRecords(
    taskId,
    page,
    PAGE_SIZE,
    filters,
  );

  if (taskId == null) {
    return (
      <TableFrame>
        <div className="text-sm text-muted-foreground">
          Select a task to see usage records.
        </div>
      </TableFrame>
    );
  }

  if (isLoading) {
    return (
      <TableFrame>
        <Skeleton className="h-40 w-full" />
      </TableFrame>
    );
  }

  if (isError || !data) {
    return (
      <TableFrame>
        <div className="text-sm text-destructive">
          Failed to load usage records
          {error instanceof Error ? `: ${error.message}` : ""}.
        </div>
      </TableFrame>
    );
  }

  const rows = data.items;
  const hasMore = data.has_more === true;
  const totalCount = data.total_count;

  return (
    <TableFrame>
      {rows.length === 0 ? (
        <div className="text-sm text-muted-foreground">
          No usage records match the current filters.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                {COLUMNS.map((col) => (
                  <TableHead
                    key={col.key}
                    className={col.numeric ? "text-right" : undefined}
                  >
                    {col.header}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((record) => (
                <TableRow key={record.id}>
                  {COLUMNS.map((col) => (
                    <TableCell
                      key={col.key}
                      className={col.numeric ? "text-right" : undefined}
                    >
                      {col.render(record)}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <div className="mt-4 flex items-center justify-between gap-3">
        <div
          className="text-xs text-muted-foreground"
          data-testid="usage-records-total"
        >
          {totalCount.toLocaleString()} record{totalCount === 1 ? "" : "s"} ·
          page {data.page}
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            data-testid="usage-records-prev"
          >
            Previous
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPage((p) => p + 1)}
            disabled={!hasMore}
            data-testid="usage-records-next"
          >
            Next
          </Button>
        </div>
      </div>
    </TableFrame>
  );
}

export default UsageRecordsTable;
