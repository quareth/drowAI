/* Visibility reducer for collapse/expand state in network-scoped topology groups. */

import type {
  TopologyCollapseState,
  TopologyGraph,
  TopologyVisibilityResult,
} from "@/components/engagements/territory/topology-types";

export function resolveTopologyVisibility(
  graph: TopologyGraph,
  collapsedGroupIds: TopologyCollapseState,
): TopologyVisibilityResult {
  const memberCounts: Record<string, number> = {};
  for (const group of Object.values(graph.groups)) {
    memberCounts[group.id] = group.memberNodeIds.length;
  }

  const visibleNodeIds = new Set<string>();
  for (const node of graph.nodes) {
    if (node.kind === "network") {
      visibleNodeIds.add(node.id);
      continue;
    }
    if (!node.networkId) {
      visibleNodeIds.add(node.id);
      continue;
    }
    if (!collapsedGroupIds.has(node.networkId)) {
      visibleNodeIds.add(node.id);
    }
  }

  const visibleNodes = graph.nodes.filter((node) => visibleNodeIds.has(node.id));
  const visibleEdges = graph.edges.filter(
    (edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target),
  );

  return {
    nodes: visibleNodes,
    edges: visibleEdges,
    memberCounts,
  };
}
