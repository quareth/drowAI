/* Shared severity presentation contract for engagement knowledge surfaces.
 *
 * This module centralizes severity vocabulary, ordering, labels, filters, and
 * badge classes so Summary, Findings, Detail, and Territory cannot drift. */

import {
  engagementIndicatorToneClass,
  type EngagementIndicatorTone,
} from "@/components/engagements/engagement-indicator-presentation";

export const FINDING_SEVERITY_LEVELS = [
  "critical",
  "high",
  "medium",
  "low",
  "info",
] as const;

export type FindingSeverityLevel = (typeof FINDING_SEVERITY_LEVELS)[number];
export type SeverityTone = FindingSeverityLevel | "unknown";

export const UNKNOWN_SEVERITY = "unknown";

export const FINDING_SEVERITY_FILTER_OPTIONS: ReadonlyArray<{
  value: FindingSeverityLevel;
  label: string;
}> = FINDING_SEVERITY_LEVELS.map((value) => ({
  value,
  label: formatSeverityLabel(value),
}));

const SEVERITY_RANK: Record<SeverityTone, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
  unknown: 5,
};

const SEVERITY_INDICATOR_TONE: Record<SeverityTone, EngagementIndicatorTone> = {
  critical: "severityCritical",
  high: "severityHigh",
  medium: "severityMedium",
  low: "severityLow",
  info: "severityInfo",
  unknown: "neutral",
};

export function normalizeSeverity(value: unknown): string {
  if (typeof value !== "string") {
    return UNKNOWN_SEVERITY;
  }
  const normalized = value.trim().toLowerCase();
  return normalized || UNKNOWN_SEVERITY;
}

export function severityTone(value: unknown): SeverityTone {
  const normalized = normalizeSeverity(value);
  return isKnownSeverity(normalized) ? normalized : UNKNOWN_SEVERITY;
}

export function severityRank(value: unknown): number {
  return SEVERITY_RANK[severityTone(value)];
}

export function compareSeverity(left: unknown, right: unknown): number {
  return severityRank(left) - severityRank(right);
}

export function formatSeverityLabel(value: unknown): string {
  const normalized = normalizeSeverity(value);
  return `${normalized.charAt(0).toUpperCase()}${normalized.slice(1)}`;
}

export function severityBadgeClass(value: unknown): string {
  return engagementIndicatorToneClass(severityIndicatorTone(value));
}

export function severityIndicatorTone(value: unknown): EngagementIndicatorTone {
  return SEVERITY_INDICATOR_TONE[severityTone(value)];
}

function isKnownSeverity(value: string): value is FindingSeverityLevel {
  return (FINDING_SEVERITY_LEVELS as readonly string[]).includes(value);
}
