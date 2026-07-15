/* Analyst threat dashboard derived from existing engagement knowledge, evidence, and task state. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Clock3,
  Database,
  FileSearch,
  RefreshCw,
  Shield,
  Target,
} from "lucide-react";

import {
  engagementSelectTriggerClass,
} from "@/components/engagements/engagement-ui";
import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import {
  FindingSeverityBadge,
  FindingStatusBadge,
} from "@/components/engagements/finding-badges";
import {
  FINDING_SEVERITY_LEVELS,
  formatSeverityLabel,
  severityIndicatorTone,
} from "@/components/engagements/severity-presentation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  useEngagementAssets,
  useEngagementFindings,
  useEngagementKnowledgeRefresh,
  useEngagements,
  useEngagementSummary,
} from "@/hooks/use-engagement-knowledge";
import { useTaskManagement } from "@/hooks/useTaskManagement";
import { useUserTimezone } from "@/hooks/use-user-timezone";
import { cn } from "@/lib/utils";
import type { EngagementSummary, FindingListItem } from "@/types/engagement-knowledge";
import { formatDateTime, formatRelative } from "@/utils/datetime";

const ACTIVE_TASK_STATUSES = new Set(["queued", "starting", "running", "in_progress", "active"]);
const dashboardPanelClass =
  "rounded-lg border border-slate-800/90 bg-slate-950/72 shadow-[0_12px_30px_-24px_rgba(15,23,42,0.95)]";
const dashboardInsetClass = "rounded-lg border border-slate-800/80 bg-slate-950/72";

function normalizeId(value: string | number | null | undefined): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  const normalized = String(value).trim();
  return normalized.length > 0 ? normalized : null;
}

function getRiskPosture(summary: EngagementSummary | undefined) {
  const severityCounts = summary?.open_findings_by_severity ?? {};
  const exploitedAssets = summary?.asset_counts.exploited ?? 0;

  if ((severityCounts.critical ?? 0) > 0 || exploitedAssets > 0) {
    return {
      label: "Critical",
      detail: exploitedAssets > 0 ? `${exploitedAssets} exploited assets` : "Critical findings open",
      className: "border-slate-700/80 bg-slate-950/72 text-slate-100",
      iconClassName: "text-slate-300",
    };
  }

  if ((severityCounts.high ?? 0) > 0) {
    return {
      label: "High",
      detail: "High severity findings open",
      className: "border-slate-700/80 bg-slate-950/72 text-slate-100",
      iconClassName: "text-slate-300",
    };
  }

  if ((severityCounts.medium ?? 0) > 0 || (summary?.asset_counts.vulnerable ?? 0) > 0) {
    return {
      label: "Elevated",
      detail: "Exposure needs analyst review",
      className: "border-slate-700/80 bg-slate-950/72 text-slate-100",
      iconClassName: "text-slate-300",
    };
  }

  return {
    label: "Baseline",
    detail: "No open high-impact findings",
    className: "border-slate-700/80 bg-slate-950/72 text-slate-100",
    iconClassName: "text-slate-300",
  };
}

function formatAssetName(asset: {
  display_name?: string | null;
  hostname?: string | null;
  ip_address?: string | null;
  asset_key?: string | null;
  id: string;
}) {
  return asset.display_name || asset.hostname || asset.ip_address || asset.asset_key || asset.id;
}

function getFindingTitle(finding: FindingListItem): string {
  return finding.title || finding.finding_key || "Untitled finding";
}

function getFindingAssetContext(finding: FindingListItem): string | null {
  const affectedAssetCount = finding.affected_asset_count ?? (finding.asset_id ? 1 : 0);
  if (affectedAssetCount > 1) {
    return `${affectedAssetCount} assets`;
  }
  if (finding.asset) {
    return formatAssetName(finding.asset);
  }
  if (finding.asset_id) {
    return finding.asset_id;
  }
  return null;
}

function formatSourceToolName(value: string | null | undefined): string {
  const normalized = (value || "").trim();
  if (!normalized) {
    return "unknown source";
  }
  return normalized.split(".").filter(Boolean).at(-1) || normalized;
}

function getFindingContextLine(finding: FindingListItem): string {
  return [
    getFindingAssetContext(finding),
    formatSourceToolName(finding.source_tool),
    formatRelative(finding.last_seen_at),
  ]
    .filter(Boolean)
    .join(" · ");
}

function summarizeOpenStatuses(statuses: string[] | undefined, openFindingsTotal: number): string {
  if (openFindingsTotal <= 0) {
    return "No open findings";
  }
  const visibleStatuses = (statuses ?? []).filter(Boolean).slice(0, 3);
  if (visibleStatuses.length === 0) {
    return "Open states active";
  }
  const suffix = (statuses?.length ?? 0) > visibleStatuses.length ? " +" : "";
  return `${visibleStatuses.join(", ")}${suffix}`;
}

function InsightCard({
  label,
  value,
  detail,
  icon: Icon,
  tone = "text-slate-200",
}: {
  label: string;
  value: string | number;
  detail: string;
  icon: typeof Shield;
  tone?: string;
}) {
  return (
    <Card className={cn(dashboardPanelClass, "bg-slate-950/62")}>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">{label}</p>
            <p className={cn("mt-2 text-2xl font-semibold", tone)}>{value}</p>
            <p className="mt-1 truncate text-xs text-slate-500">{detail}</p>
          </div>
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-slate-800 bg-slate-900/80">
            <Icon className="h-4 w-4 text-emerald-300" />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function SectionShell({
  title,
  icon: Icon,
  children,
  className,
}: {
  title: string;
  icon: typeof Shield;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn(dashboardPanelClass, className)}>
      <CardHeader className="p-4 pb-3">
        <CardTitle className="flex items-center gap-2 text-sm font-semibold text-slate-100">
          <Icon className="h-4 w-4 text-emerald-300" />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="p-4 pt-0">{children}</CardContent>
    </Card>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className={cn(dashboardInsetClass, "px-3 py-2.5 text-sm text-slate-400")}>
      {children}
    </div>
  );
}

function LoadingRows({ count = 4 }: { count?: number }) {
  return (
    <div className="space-y-2" aria-label="threat-dashboard-loading">
      {Array.from({ length: count }).map((_, index) => (
        <div key={index} className="h-12 animate-pulse rounded-lg border border-slate-800/80 bg-slate-900/80" />
      ))}
    </div>
  );
}

export function ThreatDashboardPanel() {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const timezone = useUserTimezone();
  const [selectedEngagementId, setSelectedEngagementId] = useState<string | null>(null);

  const engagementsQuery = useEngagements({ status: "active", limit: 25, offset: 0 });
  const engagements = engagementsQuery.data?.items ?? [];
  const firstEngagementId = normalizeId(engagements[0]?.id);
  const activeEngagementId = selectedEngagementId ?? firstEngagementId;

  const summaryQuery = useEngagementSummary(activeEngagementId);
  const findingsQuery = useEngagementFindings(activeEngagementId, {
    sort: "severity_desc",
    include_candidates: false,
    limit: 6,
    offset: 0,
  });
  const assetsQuery = useEngagementAssets(activeEngagementId, {
    vulnerable: true,
    sort: "last_seen_desc",
    limit: 5,
    offset: 0,
  });
  const { refresh, isRefreshing } = useEngagementKnowledgeRefresh(activeEngagementId);
  const { tasks, isLoading: tasksLoading } = useTaskManagement();

  useEffect(() => {
    if (!selectedEngagementId && firstEngagementId) {
      setSelectedEngagementId(firstEngagementId);
      return;
    }

    if (
      selectedEngagementId &&
      engagements.length > 0 &&
      !engagements.some((engagement) => normalizeId(engagement.id) === selectedEngagementId)
    ) {
      setSelectedEngagementId(firstEngagementId);
    }
  }, [engagements, firstEngagementId, selectedEngagementId]);

  const selectedEngagement = engagements.find(
    (engagement) => normalizeId(engagement.id) === activeEngagementId,
  );
  const summary = summaryQuery.data;
  const findings = findingsQuery.data?.items ?? [];
  const assets = assetsQuery.data?.items ?? [];
  const riskPosture = getRiskPosture(summary);

  const engagementTasks = useMemo(
    () =>
      tasks.filter((task) => {
        if (!activeEngagementId) {
          return false;
        }
        return normalizeId(task.engagement_id) === activeEngagementId;
      }),
    [activeEngagementId, tasks],
  );

  const activeTaskCount = engagementTasks.filter((task) =>
    ACTIVE_TASK_STATUSES.has(String(task.status || "").toLowerCase()),
  ).length;

  const latestTask = engagementTasks
    .slice()
    .sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime())[0];

  const totalOpenFindings = summary?.open_findings_total ?? 0;
  const severityTotal = FINDING_SEVERITY_LEVELS.reduce(
    (total, severity) => total + (summary?.open_findings_by_severity?.[severity] ?? 0),
    0,
  );
  const vulnerableAssets = summary?.asset_counts.vulnerable ?? 0;
  const totalAssets = summary?.asset_counts.total ?? 0;
  const evidenceCount = summary?.evidence_count ?? 0;
  const lastObservedLabel = summary?.last_observed_at
    ? formatRelative(summary.last_observed_at)
    : "No observations";
  const openStatusDetail = summarizeOpenStatuses(summary?.open_statuses, totalOpenFindings);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [activeEngagementId]);

  if (!engagementsQuery.isLoading && engagements.length === 0) {
    return (
      <div className="h-full overflow-y-auto bg-slate-950 p-6">
        <div className={cn(dashboardPanelClass, "mx-auto max-w-3xl p-8 text-center")}>
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-md border border-slate-700 bg-slate-900">
            <Shield className="h-5 w-5 text-emerald-300" />
          </div>
          <h2 className="mt-4 text-xl font-semibold text-white">Threat Dashboard</h2>
          <p className="mt-2 text-sm text-slate-400">
            Create or restore an active engagement to populate analyst insights.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="h-full overflow-y-auto bg-slate-950">
      <div className="space-y-4 p-4 lg:p-5">
        <div className={cn(dashboardPanelClass, "overflow-hidden")}>
          <div className="flex flex-col gap-4 border-b border-slate-800/80 bg-slate-950/84 p-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <div className="flex h-9 w-9 items-center justify-center rounded-md border border-emerald-500/30 bg-emerald-950/40">
                  <Shield className="h-4 w-4 text-emerald-300" />
                </div>
                <div>
                  <h2 className="text-lg font-semibold text-white">Threat Dashboard</h2>
                  <p className="text-sm text-slate-400">
                    {selectedEngagement?.name || "Loading engagement"} · {lastObservedLabel}
                  </p>
                </div>
              </div>
            </div>

            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <Select
                value={activeEngagementId ?? ""}
                onValueChange={(value) => setSelectedEngagementId(value)}
                disabled={engagementsQuery.isLoading || engagements.length === 0}
              >
                <SelectTrigger
                  aria-label="Engagement"
                  className={cn(engagementSelectTriggerClass, "w-full min-w-[220px] sm:w-[280px]")}
                >
                  <SelectValue placeholder="Select engagement" />
                </SelectTrigger>
                <SelectContent>
                  {engagements.map((engagement) => (
                    <SelectItem key={engagement.id} value={String(engagement.id)}>
                      {engagement.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void refresh()}
                disabled={!activeEngagementId || isRefreshing}
                className="border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800 hover:text-white"
              >
                <RefreshCw className={cn("mr-2 h-4 w-4", isRefreshing && "animate-spin")} />
                Refresh
              </Button>
            </div>
          </div>

          <div className="grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-4">
            <Card className={cn("rounded-lg border shadow-sm", riskPosture.className)}>
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-xs font-semibold uppercase tracking-wide opacity-80">Risk Posture</p>
                    <p className="mt-2 text-2xl font-semibold">{riskPosture.label}</p>
                    <p className="mt-1 text-xs opacity-80">{riskPosture.detail}</p>
                  </div>
                  <Target className={cn("h-6 w-6", riskPosture.iconClassName)} />
                </div>
              </CardContent>
            </Card>
            <InsightCard
              label="Open Findings"
              value={totalOpenFindings}
              detail={openStatusDetail}
              icon={AlertTriangle}
              tone={totalOpenFindings > 0 ? "text-orange-200" : "text-emerald-200"}
            />
            <InsightCard
              label="Exposed Assets"
              value={`${vulnerableAssets}/${totalAssets}`}
              detail={`${summary?.asset_counts.exploited ?? 0} exploited`}
              icon={Activity}
              tone={vulnerableAssets > 0 ? "text-amber-200" : "text-slate-200"}
            />
            <InsightCard
              label="Evidence Intake"
              value={evidenceCount}
              detail={`${summary?.relationship_count ?? 0} relationships`}
              icon={Database}
            />
          </div>
        </div>

        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(360px,0.75fr)]">
          <SectionShell title="Severity Distribution" icon={BarChart3}>
            {summaryQuery.isLoading ? (
              <LoadingRows count={5} />
            ) : severityTotal === 0 ? (
              <div className="grid gap-2 sm:grid-cols-5">
                {FINDING_SEVERITY_LEVELS.map((severity) => (
                  <div key={severity} className={cn(dashboardInsetClass, "flex items-center justify-between px-3 py-2")}>
                    <span className="text-xs text-slate-400">{formatSeverityLabel(severity)}</span>
                    <span className="text-sm font-medium text-slate-200">0</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="space-y-3">
                {FINDING_SEVERITY_LEVELS.map((severity) => {
                  const count = summary?.open_findings_by_severity?.[severity] ?? 0;
                  const percentage = severityTotal > 0 ? Math.round((count / severityTotal) * 100) : 0;
                  return (
                    <div key={severity} className="grid grid-cols-[88px_minmax(0,1fr)_48px] items-center gap-3">
                      <EngagementIndicatorBadge
                        className="justify-center"
                        tone={severityIndicatorTone(severity)}
                      >
                        {formatSeverityLabel(severity)}
                      </EngagementIndicatorBadge>
                      <Progress value={percentage} className="h-2 bg-slate-800" />
                      <span className="text-right text-sm font-medium text-slate-200">{count}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </SectionShell>

          <SectionShell title="Task Activity" icon={Clock3}>
            {tasksLoading ? (
              <LoadingRows count={3} />
            ) : (
              <div className="grid grid-cols-3 gap-2">
                <div className={cn(dashboardInsetClass, "p-3")}>
                  <p className="text-xs text-slate-500">Active</p>
                  <p className="mt-1 text-xl font-semibold text-white">{activeTaskCount}</p>
                </div>
                <div className={cn(dashboardInsetClass, "p-3")}>
                  <p className="text-xs text-slate-500">Total</p>
                  <p className="mt-1 text-xl font-semibold text-white">{engagementTasks.length}</p>
                </div>
                <div className={cn(dashboardInsetClass, "p-3")}>
                  <p className="text-xs text-slate-500">Latest</p>
                  <p className="mt-1 truncate text-sm font-medium text-slate-200">
                    {latestTask ? formatRelative(latestTask.updated_at) : "None"}
                  </p>
                </div>
              </div>
            )}
            {latestTask ? (
              <div className={cn(dashboardInsetClass, "mt-3 p-3")}>
                <div className="flex items-center justify-between gap-3">
                  <p className="truncate text-sm font-medium text-slate-100">{latestTask.name}</p>
                  <EngagementIndicatorBadge>
                    {latestTask.status}
                  </EngagementIndicatorBadge>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  Updated {formatDateTime(latestTask.updated_at, timezone)}
                </p>
              </div>
            ) : null}
          </SectionShell>
        </div>

        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(320px,0.9fr)]">
          <SectionShell title="Hot Findings" icon={FileSearch}>
            {findingsQuery.isLoading ? (
              <LoadingRows count={5} />
            ) : findings.length === 0 ? (
              <EmptyState>No open findings in this engagement.</EmptyState>
            ) : (
              <div className="space-y-2">
                {findings.map((finding) => (
                  <div
                    key={finding.id}
                    className={cn(dashboardInsetClass, "p-3 transition-colors hover:bg-slate-900/80")}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-slate-100">{getFindingTitle(finding)}</p>
                        <p className="mt-1 text-xs text-slate-500">
                          {getFindingContextLine(finding)}
                        </p>
                      </div>
                      <FindingSeverityBadge severity={finding.severity} />
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <EngagementIndicatorBadge>
                        {finding.evidence_count} evidence
                      </EngagementIndicatorBadge>
                      <FindingStatusBadge
                        isExploited={finding.is_exploited}
                        status={finding.status || "open"}
                      />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </SectionShell>

          <div className="grid gap-4">
            <SectionShell title="Exposed Assets" icon={Target}>
              {assetsQuery.isLoading ? (
                <LoadingRows count={4} />
              ) : assets.length === 0 ? (
                <EmptyState>No vulnerable assets currently projected.</EmptyState>
              ) : (
                <div className="space-y-2">
                  {assets.map((asset) => (
                    <div key={asset.id} className={cn(dashboardInsetClass, "flex items-center justify-between gap-3 p-3")}>
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-slate-100">{formatAssetName(asset)}</p>
                        <p className="mt-1 text-xs text-slate-500">
                          {asset.asset_type || "asset"} · {asset.service_count} services
                        </p>
                      </div>
                      <EngagementIndicatorBadge>
                        {asset.finding_count}
                      </EngagementIndicatorBadge>
                    </div>
                  ))}
                </div>
              )}
            </SectionShell>

          </div>
        </div>
      </div>
    </div>
  );
}
