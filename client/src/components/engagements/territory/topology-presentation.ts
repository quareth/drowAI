/* Shared presentation contract for Territory topology sizing, labels, and severity styling. */

import type {
  TopologyFindingBadge,
  TopologyNode,
  TopologyNodeKind,
} from "@/components/engagements/territory/topology-types";
import {
  formatSeverityLabel,
  severityBadgeClass as sharedSeverityBadgeClass,
  severityIndicatorTone as sharedSeverityIndicatorTone,
  severityRank as sharedSeverityRank,
  severityTone,
} from "@/components/engagements/severity-presentation";
import type { EngagementIndicatorTone } from "@/components/engagements/engagement-indicator-presentation";

export const TOPOLOGY_CANVAS_HEIGHT = 640;
export const MAX_VISIBLE_SERVICE_CHIPS = 2;
export const MAX_VISIBLE_FINDING_BADGES = 1;
export const SERVICE_CHIP_MAX_WIDTH = 132;

export interface TopologyNodeDimensions {
  width: number;
  height: number;
}

const BASE_NODE_DIMENSIONS: Record<TopologyNodeKind, TopologyNodeDimensions> = {
  network: { width: 280, height: 72 },
  asset: { width: 230, height: 84 },
  service: { width: 180, height: 56 },
  finding: { width: 210, height: 62 },
};

const ASSET_NODE_WIDTH = {
  compact: 230,
  detailed: 270,
};

const ASSET_MIN_HEIGHT = 84;
const ASSET_VERTICAL_PADDING_HEIGHT = 16;
const ASSET_HEADER_BLOCK_HEIGHT = 42;
const ASSET_RISK_FLAGS_BLOCK_HEIGHT = 26;
const ASSET_FINDINGS_BLOCK_HEIGHT = 30;
const ASSET_SERVICES_BLOCK_HEIGHT = 58;

export function resolveTopologyBoolean(
  metadata: Record<string, unknown>,
  key: string,
): boolean {
  const value = metadata[key];
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    return value.trim().toLowerCase() === "true";
  }
  if (typeof value === "number") {
    return value === 1;
  }
  return false;
}

export function severityRank(severity: string): number {
  return sharedSeverityRank(severity);
}

export function highestFindingSeverity(
  findings: TopologyFindingBadge[],
): string | null {
  if (findings.length === 0) {
    return null;
  }
  return [...findings].sort(
    (a, b) => severityRank(a.severity) - severityRank(b.severity),
  )[0].severity;
}

export function formatSeverity(severity: string): string {
  return formatSeverityLabel(severity);
}

export function compactFindingLabel(label: string): string {
  const normalized = label.replace(/\s+/g, " ").trim();
  if (/credential material exposed/i.test(normalized)) {
    return "Credential exposure";
  }
  if (/password.+expos|expos.+password/i.test(normalized)) {
    return "Password exposure";
  }
  if (/weak.+auth/i.test(normalized)) {
    return "Weak authentication";
  }
  if (normalized.length <= 28) {
    return normalized;
  }
  return `${normalized.slice(0, 25).trim()}...`;
}

export function severityBadgeClass(severity: string): string {
  return sharedSeverityBadgeClass(severity);
}

export function severityIndicatorTone(severity: string): EngagementIndicatorTone {
  return sharedSeverityIndicatorTone(severity);
}

export function assetAccentClass(
  severity: string | null,
  _vulnerable: boolean,
  _exploited: boolean,
): string {
  switch (severityTone(severity)) {
    case "critical":
      return "border-l-red-500";
    case "high":
      return "border-l-orange-400";
    case "medium":
      return "border-l-amber-400";
    case "low":
      return "border-l-cyan-400";
    case "info":
      return "border-l-slate-500";
    default:
      return "border-l-slate-500";
  }
}

export function getTopologyNodeDimensions(node: TopologyNode): TopologyNodeDimensions {
  if (node.kind !== "asset") {
    return BASE_NODE_DIMENSIONS[node.kind];
  }

  const hasRiskFlags =
    resolveTopologyBoolean(node.metadata, "is_vulnerable") ||
    resolveTopologyBoolean(node.metadata, "is_exploited");
  const hasServices = node.childServices.length > 0;
  const hasFindings = node.childFindings.length > 0;
  const height =
    ASSET_VERTICAL_PADDING_HEIGHT +
    ASSET_HEADER_BLOCK_HEIGHT +
    (hasRiskFlags ? ASSET_RISK_FLAGS_BLOCK_HEIGHT : 0) +
    (hasFindings ? ASSET_FINDINGS_BLOCK_HEIGHT : 0) +
    (hasServices ? ASSET_SERVICES_BLOCK_HEIGHT : 0);

  return {
    width: hasServices || hasFindings ? ASSET_NODE_WIDTH.detailed : ASSET_NODE_WIDTH.compact,
    height: Math.max(ASSET_MIN_HEIGHT, height),
  };
}
