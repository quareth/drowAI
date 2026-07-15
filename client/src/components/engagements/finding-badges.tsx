/* Reusable finding badges that keep table and detail-panel presentation aligned. */

import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import {
  formatFindingStatusLabel,
  normalizeFindingStatus,
  resolveFindingDisplayStatus,
} from "@/components/engagements/finding-presentation";
import {
  formatSeverityLabel,
  normalizeSeverity,
  severityIndicatorTone,
} from "@/components/engagements/severity-presentation";

interface FindingSeverityBadgeProps {
  severity: unknown;
}

interface FindingStatusBadgeProps {
  isExploited?: boolean;
  status: unknown;
}

export function FindingSeverityBadge({ severity }: FindingSeverityBadgeProps) {
  const normalized = normalizeSeverity(severity);
  const label = formatSeverityLabel(severity);

  return (
    <EngagementIndicatorBadge
      label={`severity-${normalized}`}
      tone={severityIndicatorTone(severity)}
    >
      {label}
    </EngagementIndicatorBadge>
  );
}

export function FindingStatusBadge({ isExploited = false, status }: FindingStatusBadgeProps) {
  const displayStatus = resolveFindingDisplayStatus({ status, isExploited });
  const normalized = normalizeFindingStatus(displayStatus);
  const label = formatFindingStatusLabel(displayStatus);

  return (
    <EngagementIndicatorBadge
      className="max-w-full truncate"
      label={`finding-status-${normalized}`}
      title={label}
    >
      {label}
    </EngagementIndicatorBadge>
  );
}
