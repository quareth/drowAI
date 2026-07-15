/* Shared finding presentation helpers for status normalization and labels. */

export const UNKNOWN_FINDING_STATUS = "unknown";
export const EXPLOITED_FINDING_STATUS = "exploited";

export interface FindingDisplayStatusInput {
  status?: unknown;
  is_exploited?: unknown;
  isExploited?: unknown;
}

export function normalizeFindingStatus(value: unknown): string {
  if (typeof value !== "string") {
    return UNKNOWN_FINDING_STATUS;
  }
  const normalized = value.trim().toLowerCase();
  return normalized || UNKNOWN_FINDING_STATUS;
}

export function formatFindingStatusLabel(value: unknown): string {
  const normalized = normalizeFindingStatus(value);
  return normalized
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((segment) => `${segment.charAt(0).toUpperCase()}${segment.slice(1)}`)
    .join(" ");
}

export function resolveFindingDisplayStatus(input: FindingDisplayStatusInput): string {
  if (input.is_exploited === true || input.isExploited === true) {
    return EXPLOITED_FINDING_STATUS;
  }
  return normalizeFindingStatus(input.status);
}
