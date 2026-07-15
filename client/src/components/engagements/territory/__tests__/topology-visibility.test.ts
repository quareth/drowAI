// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { resolveTopologyVisibility } from "@/components/engagements/territory/topology-visibility";
import type { TopologyGraph } from "@/components/engagements/territory/topology-types";

const baseGraph: TopologyGraph = {
  engagementId: 1,
  source: "graph",
  nodes: [
    {
      id: "network:corp",
      label: "Corp",
      kind: "network",
      metadata: {},
      networkId: null,
      sourceNode: null,
      synthetic: false,
      childServices: [],
      childFindings: [],
    },
    {
      id: "asset:1",
      label: "Host A",
      kind: "asset",
      metadata: {},
      networkId: "network:corp",
      sourceNode: null,
      synthetic: false,
      childServices: [],
      childFindings: [],
    },
  ],
  edges: [
    {
      id: "contains:1",
      source: "network:corp",
      target: "asset:1",
      kind: "contains",
      label: "contains",
      metadata: {},
      sourceEdge: null,
      synthetic: false,
    },
  ],
  groups: {
    "network:corp": {
      id: "network:corp",
      label: "Corp",
      memberNodeIds: ["asset:1"],
    },
  },
};

describe("topology-visibility", () => {
  it("hides group members when network is collapsed", () => {
    const collapsed = resolveTopologyVisibility(baseGraph, new Set(["network:corp"]));
    expect(collapsed.nodes.map((node) => node.id)).toEqual(["network:corp"]);
    expect(collapsed.edges.length).toBe(0);
  });

  it("keeps members visible when network is expanded", () => {
    const expanded = resolveTopologyVisibility(baseGraph, new Set());
    expect(expanded.nodes.length).toBe(2);
    expect(expanded.edges.length).toBe(1);
    expect(expanded.memberCounts["network:corp"]).toBe(1);
  });
});
