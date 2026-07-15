/* Compound asset node renderer that embeds compact service and finding summaries. */

import type { Node as FlowNode, NodeProps } from "@xyflow/react";
import { Handle, Position } from "@xyflow/react";
import { Server, TriangleAlert, Waypoints } from "lucide-react";

import { cn } from "@/lib/utils";
import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import { ServiceChip } from "@/components/engagements/territory/service-chip";
import {
  assetAccentClass,
  compactFindingLabel,
  formatSeverity,
  getTopologyNodeDimensions,
  highestFindingSeverity,
  MAX_VISIBLE_FINDING_BADGES,
  MAX_VISIBLE_SERVICE_CHIPS,
  resolveTopologyBoolean,
  severityIndicatorTone,
} from "@/components/engagements/territory/topology-presentation";
import type {
  TopologyFindingBadge,
  TopologyNodeRenderData,
} from "@/components/engagements/territory/topology-types";

function FindingBadge({ finding }: { finding: TopologyFindingBadge }) {
  const compactLabel = compactFindingLabel(finding.label);
  return (
    <EngagementIndicatorBadge
      size="xs"
      className="max-w-full gap-1"
      title={`${formatSeverity(finding.severity)}: ${finding.label}`}
      tone={severityIndicatorTone(finding.severity)}
      data-testid={`finding-badge-${finding.id}`}
    >
      <TriangleAlert className="h-2.5 w-2.5 shrink-0" />
      <span className="truncate">
        {formatSeverity(finding.severity)}: {compactLabel}
      </span>
    </EngagementIndicatorBadge>
  );
}

function OverflowButton({
  count,
  label,
  onClick,
}: {
  count: number;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className="inline-flex items-center rounded-md border border-slate-600/50 bg-slate-900/70 px-1.5 py-0.5 text-[10px] text-slate-300 hover:border-emerald-500/60 hover:text-emerald-200"
      aria-label={label}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClick();
      }}
    >
      +{count}
    </button>
  );
}

export function AssetNode({
  data,
  selected,
}: NodeProps<FlowNode<TopologyNodeRenderData>>) {
  const { node, onSelectAsset, onSelectService } = data;
  const isVulnerable = resolveTopologyBoolean(node.metadata, "is_vulnerable");
  const isExploited = resolveTopologyBoolean(node.metadata, "is_exploited");
  const services = node.childServices;
  const findings = node.childFindings;
  const visibleChips = services.slice(0, MAX_VISIBLE_SERVICE_CHIPS);
  const serviceOverflowCount = services.length - visibleChips.length;
  const visibleFindings = findings.slice(0, MAX_VISIBLE_FINDING_BADGES);
  const findingOverflowCount = findings.length - visibleFindings.length;
  const severity = highestFindingSeverity(findings);
  const hasRiskFlags = isVulnerable || isExploited;
  const dimensions = getTopologyNodeDimensions(node);

  return (
    <div
      className={cn(
        "overflow-hidden rounded-md border border-l-4 px-3 py-2 text-xs text-slate-100 shadow-[0_8px_24px_-18px_rgba(15,23,42,0.95)]",
        "border-slate-600/90 bg-slate-900/90",
        assetAccentClass(severity, isVulnerable, isExploited),
        selected && "ring-2 ring-emerald-400/70",
      )}
      style={{ width: dimensions.width, height: dimensions.height }}
      data-testid={`territory-node-${node.id}`}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="h-2.5 w-2.5 border-slate-300/40 bg-slate-900/80"
      />

      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <Server className="h-3.5 w-3.5 text-slate-300" />
            <p className="truncate font-semibold text-slate-100">{node.label}</p>
          </div>
          <p className="mt-1 text-[10px] uppercase tracking-wide text-slate-400">
            asset
          </p>
        </div>
        <div className="shrink-0 text-right text-[10px] text-slate-400">
          {services.length > 0 && <p>{services.length} svc</p>}
          {findings.length > 0 && <p>{findings.length} finding{findings.length === 1 ? "" : "s"}</p>}
        </div>
      </div>

      {hasRiskFlags && (
        <div className="mt-1 flex flex-wrap gap-1">
          {isVulnerable && (
            <EngagementIndicatorBadge size="xs">vulnerable</EngagementIndicatorBadge>
          )}
          {isExploited && (
            <EngagementIndicatorBadge size="xs">exploited</EngagementIndicatorBadge>
          )}
        </div>
      )}

      {visibleFindings.length > 0 && (
        <div className="mt-2 flex max-w-[260px] flex-wrap gap-1">
          {visibleFindings.map((finding) => (
            <FindingBadge key={finding.id} finding={finding} />
          ))}
          {findingOverflowCount > 0 && (
            <OverflowButton
              count={findingOverflowCount}
              label="Show all findings for asset"
              onClick={() => onSelectAsset?.(node)}
            />
          )}
        </div>
      )}

      {visibleChips.length > 0 && (
        <div className="mt-2 border-t border-slate-700/50 pt-2">
          <div className="mb-1 flex items-center gap-1 text-[10px] text-slate-400">
            <Waypoints className="h-2.5 w-2.5" />
            <span>Services ({services.length})</span>
          </div>
          <div className="flex flex-wrap gap-1">
            {visibleChips.map((chip) => (
              <ServiceChip
                key={chip.id}
                chip={chip}
                onClick={(c) => onSelectService?.(c)}
              />
            ))}
            {serviceOverflowCount > 0 && (
              <OverflowButton
                count={serviceOverflowCount}
                label="Show all services for asset"
                onClick={() => onSelectAsset?.(node)}
              />
            )}
          </div>
        </div>
      )}

      <Handle
        type="source"
        position={Position.Right}
        className="h-2.5 w-2.5 border-slate-300/40 bg-slate-900/80"
      />
    </div>
  );
}
