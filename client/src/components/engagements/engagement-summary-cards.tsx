/* Engagement-knowledge summary strip cards for engagement risk and durable evidence status. */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import { engagementCardClass } from "@/components/engagements/engagement-ui";
import {
  FINDING_SEVERITY_LEVELS,
  formatSeverityLabel,
  severityIndicatorTone,
} from "@/components/engagements/severity-presentation";
import { useUserTimezone } from "@/hooks/use-user-timezone";
import type { EngagementSummary } from "@/types/engagement-knowledge";
import { formatDateTime } from "@/utils/datetime";

interface EngagementSummaryCardsProps {
  summary?: EngagementSummary;
  isLoading?: boolean;
}

function LoadingCard({ label }: { label: string }) {
  return (
    <Card className={engagementCardClass}>
      <CardHeader className="pb-2">
        <CardTitle className="text-xs font-semibold tracking-wide text-slate-400 uppercase">
          {label}
        </CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="h-7 w-16 animate-pulse rounded bg-slate-700/80" />
      </CardContent>
    </Card>
  );
}

export function EngagementSummaryCards({
  summary,
  isLoading = false,
}: EngagementSummaryCardsProps) {
  const timezone = useUserTimezone();
  const lastObservedAt = summary?.last_observed_at;
  const formattedLastUpdated = formatDateTime(lastObservedAt, timezone);
  const lastUpdatedLabel =
    !lastObservedAt
      ? "Not observed yet"
      : formattedLastUpdated === "—"
        ? "Unknown"
        : `${formattedLastUpdated} UTC`;

  if (isLoading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-3" aria-label="summary-loading">
        <LoadingCard label="Open Findings" />
        <LoadingCard label="Assets" />
        <LoadingCard label="Services" />
        <LoadingCard label="Evidence" />
        <LoadingCard label="Last Updated" />
      </div>
    );
  }

  if (!summary) {
    return (
      <Card className={engagementCardClass}>
        <CardContent className="p-6 text-slate-300">
          No durable engagement summary available yet.
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-5">
      <Card className={engagementCardClass}>
        <CardHeader className="pb-2">
          <CardTitle className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            Open Findings
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <p className="text-2xl font-semibold text-white">{summary.open_findings_total}</p>
          <div className="mt-2 flex flex-wrap gap-1">
            {FINDING_SEVERITY_LEVELS.map((severity) => {
              const count = summary.open_findings_by_severity?.[severity] ?? 0;
              if (count === 0) {
                return null;
              }
              return (
                <EngagementIndicatorBadge
                  key={severity}
                  className="engagement-motion"
                  tone={severityIndicatorTone(severity)}
                >
                  {formatSeverityLabel(severity)}: {count}
                </EngagementIndicatorBadge>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <Card className={engagementCardClass}>
        <CardHeader className="pb-2">
          <CardTitle className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            Assets
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <p className="text-2xl font-semibold text-white">{summary.asset_counts.total}</p>
          <p className="text-xs text-slate-400 mt-2">
            Vulnerable {summary.asset_counts.vulnerable} / Exploited {summary.asset_counts.exploited}
          </p>
        </CardContent>
      </Card>

      <Card className={engagementCardClass}>
        <CardHeader className="pb-2">
          <CardTitle className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            Services
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <p className="text-2xl font-semibold text-white">{summary.service_count}</p>
        </CardContent>
      </Card>

      <Card className={engagementCardClass}>
        <CardHeader className="pb-2">
          <CardTitle className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            Evidence
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <p className="text-2xl font-semibold text-white">{summary.evidence_count}</p>
        </CardContent>
      </Card>

      <Card className={engagementCardClass}>
        <CardHeader className="pb-2">
          <CardTitle className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            Last Updated
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <p className="text-sm font-medium text-slate-200">
            {lastUpdatedLabel}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
