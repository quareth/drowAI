/**
 * Live system resource overview backed by host metrics and tenant task queries.
 */
import type { ReactNode } from "react";
import { Activity, Clock3, HardDrive, MemoryStick } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useSystemMetrics, SYSTEM_METRICS_POLL_INTERVAL_MS } from "@/hooks/use-system-metrics";
import { useTaskManagement } from "@/hooks/useTaskManagement";

const BYTES_PER_UNIT = 1024;
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;
const SECONDS_PER_MINUTE = 60;
const MINUTES_PER_HOUR = 60;
const HOURS_PER_DAY = 24;
const RUNNING_TASK_STATUS = "running";

interface MetricCardProps {
  title: string;
  value: string;
  detail: string;
  icon: ReactNode;
  usagePercent?: number;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }

  const unitIndex = Math.min(
    Math.floor(Math.log(bytes) / Math.log(BYTES_PER_UNIT)),
    BYTE_UNITS.length - 1,
  );
  const value = bytes / BYTES_PER_UNIT ** unitIndex;
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)} ${BYTE_UNITS[unitIndex]}`;
}

function formatUptime(totalSeconds: number): string {
  const wholeSeconds = Math.max(0, Math.floor(totalSeconds));
  const totalMinutes = Math.floor(wholeSeconds / SECONDS_PER_MINUTE);
  const totalHours = Math.floor(totalMinutes / MINUTES_PER_HOUR);
  const days = Math.floor(totalHours / HOURS_PER_DAY);
  const hours = totalHours % HOURS_PER_DAY;
  const minutes = totalMinutes % MINUTES_PER_HOUR;

  if (days > 0) {
    return `${days}d ${hours}h`;
  }
  if (totalHours > 0) {
    return `${totalHours}h ${minutes}m`;
  }
  if (totalMinutes > 0) {
    return `${totalMinutes}m`;
  }
  return `${wholeSeconds}s`;
}

function MetricCard({ title, value, detail, icon, usagePercent }: MetricCardProps) {
  const normalizedPercent = Math.min(100, Math.max(0, usagePercent ?? 0));

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h4 className="text-sm font-medium text-slate-300">{title}</h4>
        <span className="text-slate-500" aria-hidden="true">
          {icon}
        </span>
      </div>
      <p className="text-2xl font-semibold tracking-tight text-slate-100">{value}</p>
      <p className="mt-1 text-xs text-slate-500">{detail}</p>
      {usagePercent !== undefined ? (
        <div
          className="mt-4 h-1.5 overflow-hidden rounded-full bg-slate-800"
          role="progressbar"
          aria-label={`${title} utilization`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(normalizedPercent)}
        >
          <div
            className="h-full rounded-full bg-slate-500 transition-[width] duration-500"
            style={{ width: `${normalizedPercent}%` }}
          />
        </div>
      ) : null}
    </div>
  );
}

export function SystemSettingsPanel() {
  const metricsQuery = useSystemMetrics();
  const { tasks, isLoading: tasksLoading } = useTaskManagement({
    refetchInterval: SYSTEM_METRICS_POLL_INTERVAL_MS,
  });
  const metrics = metricsQuery.data;
  const runningTaskCount = tasks.filter(
    (task) => task.status.toLowerCase() === RUNNING_TASK_STATUS,
  ).length;
  const refreshStatus = metricsQuery.isError
    ? "Metrics temporarily unavailable"
    : metricsQuery.isFetching
      ? "Updating metrics"
      : "Live metrics";

  return (
    <Card className="border-slate-800 bg-slate-900/80 shadow-none">
      <CardHeader className="space-y-1">
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="text-lg font-semibold text-slate-100">System overview</CardTitle>
            <CardDescription className="mt-1 text-slate-500">
              Management host resources and tenant task activity
            </CardDescription>
          </div>
          <div className="flex items-center gap-2 pt-1 text-xs text-slate-500" aria-live="polite">
            <span className="h-1.5 w-1.5 rounded-full bg-slate-500" aria-hidden="true" />
            {refreshStatus}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard
            title="Memory"
            value={metrics ? formatBytes(metrics.memory.used_bytes) : "—"}
            detail={metrics ? `of ${formatBytes(metrics.memory.total_bytes)} memory` : "Loading usage"}
            usagePercent={metrics?.memory.usage_percent}
            icon={<MemoryStick className="h-4 w-4" />}
          />
          <MetricCard
            title="Storage"
            value={metrics ? formatBytes(metrics.storage.used_bytes) : "—"}
            detail={
              metrics
                ? `of ${formatBytes(metrics.storage.total_bytes)} workspace storage`
                : "Loading usage"
            }
            usagePercent={metrics?.storage.usage_percent}
            icon={<HardDrive className="h-4 w-4" />}
          />
          <MetricCard
            title="Uptime"
            value={metrics ? formatUptime(metrics.uptime_seconds) : "—"}
            detail="Management host since boot"
            icon={<Clock3 className="h-4 w-4" />}
          />
          <MetricCard
            title="Active tasks"
            value={tasksLoading ? "—" : String(runningTaskCount)}
            detail="Currently running for this tenant"
            icon={<Activity className="h-4 w-4" />}
          />
        </div>
      </CardContent>
    </Card>
  );
}
