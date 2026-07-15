/* Custom React Flow node renderers for topology entities and group collapse controls. */

import type { Node as FlowNode, NodeTypes, NodeProps } from "@xyflow/react";
import { Handle, Position } from "@xyflow/react";
import { Network, Server, TriangleAlert, Waypoints } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import { AssetNode } from "@/components/engagements/territory/asset-node";
import {
  getTopologyNodeDimensions,
  resolveTopologyBoolean,
} from "@/components/engagements/territory/topology-presentation";
import type {
  TopologyNodeKind,
  TopologyNodeRenderData,
} from "@/components/engagements/territory/topology-types";

function nodeTone(kind: TopologyNodeKind): string {
  switch (kind) {
    case "network":
      return "border-cyan-700/70 bg-cyan-950/35";
    case "service":
      return "border-blue-700/70 bg-blue-950/35";
    case "finding":
      return "border-amber-700/70 bg-amber-950/35";
    case "asset":
    default:
      return "border-slate-600/90 bg-slate-900/85";
  }
}

function NodeIcon({ kind }: { kind: TopologyNodeKind }) {
  if (kind === "network") {
    return <Network className="h-3.5 w-3.5 text-cyan-300" />;
  }
  if (kind === "service") {
    return <Waypoints className="h-3.5 w-3.5 text-blue-300" />;
  }
  if (kind === "finding") {
    return <TriangleAlert className="h-3.5 w-3.5 text-amber-300" />;
  }
  return <Server className="h-3.5 w-3.5 text-slate-300" />;
}

function BaseTopologyNode({
  data,
  selected,
}: NodeProps<FlowNode<TopologyNodeRenderData>>) {
  const { node, memberCount, isCollapsed, onToggleGroup } = data;
  const isVulnerable = resolveTopologyBoolean(node.metadata, "is_vulnerable");
  const isExploited = resolveTopologyBoolean(node.metadata, "is_exploited");
  const isNetwork = node.kind === "network";
  const dimensions = getTopologyNodeDimensions(node);

  return (
    <div
      className={cn(
        "rounded-md border px-3 py-2 text-xs text-slate-100 shadow-[0_8px_24px_-18px_rgba(15,23,42,0.95)]",
        nodeTone(node.kind),
        selected && "ring-2 ring-emerald-400/70",
      )}
      style={{ width: dimensions.width, minHeight: dimensions.height }}
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
            <NodeIcon kind={node.kind} />
            <p className="truncate font-semibold text-slate-100">{node.label}</p>
          </div>
          <p className="mt-1 text-[10px] uppercase tracking-wide text-slate-400">
            {node.kind}
          </p>
        </div>
        {isNetwork && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-5 rounded-md border-slate-500/60 bg-slate-900/70 px-1.5 text-[10px] text-slate-200 hover:bg-slate-800"
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              onToggleGroup?.(node.id);
            }}
            aria-label={isCollapsed ? "Expand network group" : "Collapse network group"}
          >
            {isCollapsed ? "+" : "-"}
          </Button>
        )}
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {isNetwork && (
          <EngagementIndicatorBadge size="xs">
            members: {memberCount}
          </EngagementIndicatorBadge>
        )}
        {isVulnerable && (
          <EngagementIndicatorBadge size="xs">vulnerable</EngagementIndicatorBadge>
        )}
        {isExploited && (
          <EngagementIndicatorBadge size="xs">exploited</EngagementIndicatorBadge>
        )}
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="h-2.5 w-2.5 border-slate-300/40 bg-slate-900/80"
      />
    </div>
  );
}

export const topologyNodeTypes: NodeTypes = {
  network: BaseTopologyNode,
  asset: AssetNode,
  service: BaseTopologyNode,
  finding: BaseTopologyNode,
};
