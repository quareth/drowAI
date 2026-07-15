/* Public exports for the engagement territory topology module. */

export { adaptEngagementGraphToTopology } from "@/components/engagements/territory/topology-adapter";
export { AssetInspectorPanel } from "@/components/engagements/territory/asset-inspector-panel";
export { TopologyCanvas } from "@/components/engagements/territory/topology-canvas";
export { WebSurfacePanel } from "@/components/engagements/territory/web-surface-panel";
export { resolveTopologyVisibility } from "@/components/engagements/territory/topology-visibility";
export type {
  TopologyGraph,
  TopologyNode,
  TopologyEdge,
  TopologyServiceChip,
  TopologyFindingBadge,
} from "@/components/engagements/territory/topology-types";
