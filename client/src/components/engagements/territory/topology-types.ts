/* Internal topology view models for Territory canvas rendering and interaction contracts. */

import type {
  EngagementGraphSnapshot,
  GraphEdge,
  GraphNode,
} from "@/types/engagement-knowledge";

export type TopologyNodeKind = "network" | "asset" | "service" | "finding";

export type TopologyEdgeKind =
  | "contains"
  | "exposes"
  | "affects"
  | "has_finding"
  | "route"
  | "related";

export interface TopologyGroup {
  id: string;
  label: string;
  memberNodeIds: string[];
}

export interface TopologyServiceChip {
  id: string;
  label: string;
  port: number | null;
  protocol: string | null;
  status: string | null;
  sourceNode: GraphNode | null;
  metadata: Record<string, unknown>;
}

export interface TopologyFindingBadge {
  id: string;
  label: string;
  severity: string;
  status: string | null;
  sourceNode: GraphNode | null;
  metadata: Record<string, unknown>;
}

export interface TopologyNode {
  id: string;
  label: string;
  kind: TopologyNodeKind;
  metadata: Record<string, unknown>;
  networkId: string | null;
  sourceNode: GraphNode | null;
  synthetic: boolean;
  childServices: TopologyServiceChip[];
  childFindings: TopologyFindingBadge[];
}

export interface TopologyEdge {
  id: string;
  source: string;
  target: string;
  kind: TopologyEdgeKind;
  label: string | null;
  metadata: Record<string, unknown>;
  sourceEdge: GraphEdge | null;
  synthetic: boolean;
}

export interface TopologyGraph {
  engagementId: number;
  source: "graph";
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  groups: Record<string, TopologyGroup>;
}

export interface TopologyView {
  graph: TopologyGraph;
  sparse: boolean;
}

export interface TopologyLayoutInput {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

export interface TopologyLayoutPosition {
  x: number;
  y: number;
}

export type TopologyCollapseState = Set<string>;

export interface TopologyVisibilityResult {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  memberCounts: Record<string, number>;
}

export interface TopologyNodeRenderData {
  [key: string]: unknown;
  node: TopologyNode;
  engagementId?: number;
  memberCount: number;
  isCollapsed: boolean;
  onToggleGroup?: (groupId: string) => void;
  onSelectAsset?: (node: TopologyNode) => void;
  onSelectService?: (service: TopologyServiceChip) => void;
}

export type TopologyEdgeRenderData = {
  [key: string]: unknown;
  edge: TopologyEdge;
};

export interface TopologyCanvasGraphSelection {
  node: GraphNode | null;
  edge: GraphEdge | null;
}

export interface TopologySourceBundle {
  graph: EngagementGraphSnapshot | undefined;
}
