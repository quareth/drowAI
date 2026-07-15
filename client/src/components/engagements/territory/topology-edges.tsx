/* Custom React Flow edge renderer for route-like topology relationships. */

import type { Edge as FlowEdge, EdgeProps, EdgeTypes } from "@xyflow/react";
import { BaseEdge, EdgeLabelRenderer, getBezierPath } from "@xyflow/react";

import type {
  TopologyEdgeKind,
  TopologyEdgeRenderData,
} from "@/components/engagements/territory/topology-types";

function edgeTone(kind: TopologyEdgeKind): { stroke: string; animated: boolean } {
  switch (kind) {
    case "route":
      return { stroke: "#38bdf8", animated: true };
    case "contains":
      return { stroke: "#64748b", animated: false };
    case "exposes":
      return { stroke: "#60a5fa", animated: true };
    case "affects":
      return { stroke: "#f59e0b", animated: true };
    case "has_finding":
      return { stroke: "#f59e0b", animated: true };
    case "related":
    default:
      return { stroke: "#94a3b8", animated: false };
  }
}

function TopologyEdge({
  id,
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
  markerEnd,
  data,
}: EdgeProps<FlowEdge<TopologyEdgeRenderData>>) {
  const edge = data?.edge;
  const tone = edgeTone(edge?.kind || "related");
  const showLabel = Boolean(edge?.label && edge.kind !== "contains");
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={{
          stroke: tone.stroke,
          strokeWidth: edge?.kind === "route" ? 2.1 : 1.6,
          opacity: 0.95,
          strokeDasharray: edge?.kind === "route" ? "6 4" : undefined,
        }}
      />
      {showLabel && (
        <EdgeLabelRenderer>
          <div
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
              pointerEvents: "none",
            }}
            className="rounded-md border border-slate-700/90 bg-slate-950/90 px-1.5 py-0.5 text-[10px] text-slate-300"
          >
            {edge?.label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export const topologyEdgeTypes: EdgeTypes = {
  topology: TopologyEdge,
};
