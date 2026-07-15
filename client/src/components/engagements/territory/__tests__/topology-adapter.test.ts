// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import {
  adaptEngagementGraphToTopology,
  groupFindingsUnderAssets,
  groupServicesUnderAssets,
} from "@/components/engagements/territory/topology-adapter";
import type { TopologyNode, TopologyEdge } from "@/components/engagements/territory/topology-types";

describe("topology-adapter", () => {
  it("maps graph nodes/edges into topology entities and inferred networks", () => {
    const { graph, sparse } = adaptEngagementGraphToTopology({
      engagement_id: 7,
      nodes: [
        {
          id: "asset-1",
          subject_key: "host.ip:10.44.12.9",
          node_type: "asset",
          label: "10.44.12.9",
          metadata: {},
        },
        {
          id: "service-1",
          subject_key: "service.socket:10.44.12.9/tcp/443",
          node_type: "service",
          label: "HTTPS:443",
          metadata: {},
        },
      ],
      edges: [
        {
          id: "edge-1",
          source: "asset-1",
          target: "service-1",
          relationship_type: "exposes",
          confidence: "high",
          first_seen_at: null,
          last_seen_at: null,
          metadata: {},
        },
      ],
    });

    const networkNodes = graph.nodes.filter((node) => node.kind === "network");
    expect(networkNodes.length).toBeGreaterThan(0);
    expect(graph.edges.some((edge) => edge.kind === "contains")).toBe(true);
    const assetNode = graph.nodes.find((n) => n.kind === "asset");
    expect(assetNode).toBeDefined();
    expect(assetNode!.childServices).toHaveLength(1);
    expect(assetNode!.childServices[0].id).toBe("service-1");
    expect(assetNode!.childServices[0].port).toBe(443);
    expect(assetNode!.childServices[0].protocol).toBe("tcp");
    expect(sparse).toBe(true);
  });

  it("absorbs services and findings into asset cards in the adapted topology", () => {
    const { graph } = adaptEngagementGraphToTopology({
      engagement_id: 7,
      nodes: [
        {
          id: "asset-1",
          subject_key: "host.ip:10.44.12.9",
          node_type: "asset",
          label: "10.44.12.9",
          metadata: {},
        },
        {
          id: "service-1",
          subject_key: "service.socket:10.44.12.9/tcp/443",
          node_type: "service",
          label: "HTTPS",
          metadata: { port: 443, protocol: "tcp" },
        },
        {
          id: "finding-1",
          subject_key: "finding.vulnerability:service.socket:10.44.12.9/tcp/443:nuclei",
          node_type: "finding",
          label: "Password exposure",
          metadata: { severity: "high" },
        },
      ],
      edges: [
        {
          id: "edge-exposes",
          source: "asset-1",
          target: "service-1",
          relationship_type: "exposes",
          confidence: "high",
          first_seen_at: null,
          last_seen_at: null,
          metadata: {},
        },
        {
          id: "edge-finding",
          source: "service-1",
          target: "finding-1",
          relationship_type: "has_finding",
          confidence: "high",
          first_seen_at: null,
          last_seen_at: null,
          metadata: {},
        },
      ],
    });

    expect(graph.nodes.some((node) => node.id === "service-1")).toBe(false);
    expect(graph.nodes.some((node) => node.id === "finding-1")).toBe(false);
    const assetNode = graph.nodes.find((node) => node.id === "asset-1");
    expect(assetNode?.childServices).toHaveLength(1);
    expect(assetNode?.childFindings).toEqual([
      expect.objectContaining({ id: "finding-1", severity: "high" }),
    ]);
    expect(graph.edges.some((edge) => edge.kind === "has_finding")).toBe(false);
  });

  it("marks sufficiently connected graphs as non-sparse", () => {
    const { sparse } = adaptEngagementGraphToTopology({
      engagement_id: 7,
      nodes: [
        {
          id: "network-1",
          subject_key: "network.cidr:10.10.10.0/24",
          node_type: "network",
          label: "10.10.10.0/24",
          metadata: {},
        },
        {
          id: "asset-1",
          subject_key: "host.ip:10.10.10.8",
          node_type: "asset",
          label: "10.10.10.8",
          metadata: { network_id: "network-1" },
        },
        {
          id: "asset-2",
          subject_key: "host.ip:10.10.10.9",
          node_type: "asset",
          label: "10.10.10.9",
          metadata: { network_id: "network-1" },
        },
        {
          id: "asset-3",
          subject_key: "host.ip:10.10.10.10",
          node_type: "asset",
          label: "10.10.10.10",
          metadata: { network_id: "network-1" },
        },
        {
          id: "finding-1",
          subject_key: "finding.vuln:10.10.10.8",
          node_type: "finding",
          label: "Weak Cipher",
          metadata: {},
        },
      ],
      edges: [
        {
          id: "edge-1",
          source: "network-1",
          target: "asset-1",
          relationship_type: "contains",
          confidence: "high",
          first_seen_at: null,
          last_seen_at: null,
          metadata: {},
        },
        {
          id: "edge-2",
          source: "network-1",
          target: "asset-2",
          relationship_type: "contains",
          confidence: "high",
          first_seen_at: null,
          last_seen_at: null,
          metadata: {},
        },
        {
          id: "edge-3",
          source: "network-1",
          target: "asset-3",
          relationship_type: "contains",
          confidence: "high",
          first_seen_at: null,
          last_seen_at: null,
          metadata: {},
        },
        {
          id: "edge-4",
          source: "finding-1",
          target: "asset-1",
          relationship_type: "affects",
          confidence: "medium",
          first_seen_at: null,
          last_seen_at: null,
          metadata: {},
        },
      ],
    });

    expect(sparse).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Phase 2: groupServicesUnderAssets tests
// ---------------------------------------------------------------------------

function makeNode(
  id: string,
  kind: TopologyNode["kind"],
  overrides: Partial<TopologyNode> = {},
): TopologyNode {
  return {
    id,
    label: id,
    kind,
    metadata: {},
    networkId: null,
    sourceNode: null,
    synthetic: false,
    childServices: [],
    childFindings: [],
    ...overrides,
  };
}

function makeEdge(
  id: string,
  source: string,
  target: string,
  kind: TopologyEdge["kind"],
): TopologyEdge {
  return {
    id,
    source,
    target,
    kind,
    label: kind,
    metadata: {},
    sourceEdge: null,
    synthetic: false,
  };
}

describe("groupServicesUnderAssets", () => {
  it("groups a service under its parent asset via exposes edge", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("svc-1", "service", { metadata: { port: 5432, protocol: "tcp" } }),
    ];
    const edges = [makeEdge("e1", "asset-1", "svc-1", "exposes")];

    const result = groupServicesUnderAssets(nodes, edges);

    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("asset-1");
    expect(result.nodes[0].childServices).toHaveLength(1);
    expect(result.nodes[0].childServices[0].id).toBe("svc-1");
    expect(result.nodes[0].childServices[0].port).toBe(5432);
    expect(result.nodes[0].childServices[0].protocol).toBe("tcp");
    expect(result.edges).toHaveLength(0);
  });

  it("keeps orphan services as standalone top-level nodes", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("svc-orphan", "service"),
    ];
    const edges = [makeEdge("e1", "asset-1", "svc-orphan", "related")];

    const result = groupServicesUnderAssets(nodes, edges);

    expect(result.nodes).toHaveLength(2);
    const serviceNode = result.nodes.find((n) => n.id === "svc-orphan");
    expect(serviceNode).toBeDefined();
    expect(serviceNode!.childServices).toHaveLength(0);
  });

  it("groups multiple services under a single asset", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("svc-1", "service"),
      makeNode("svc-2", "service"),
      makeNode("svc-3", "service"),
    ];
    const edges = [
      makeEdge("e1", "asset-1", "svc-1", "exposes"),
      makeEdge("e2", "asset-1", "svc-2", "exposes"),
      makeEdge("e3", "asset-1", "svc-3", "exposes"),
    ];

    const result = groupServicesUnderAssets(nodes, edges);

    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].childServices).toHaveLength(3);
    const chipIds = result.nodes[0].childServices.map((c) => c.id).sort();
    expect(chipIds).toEqual(["svc-1", "svc-2", "svc-3"]);
  });

  it("remaps finding edge targeting absorbed service to parent asset", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("svc-1", "service"),
      makeNode("finding-1", "finding"),
    ];
    const edges = [
      makeEdge("e-expose", "asset-1", "svc-1", "exposes"),
      makeEdge("e-finding", "finding-1", "svc-1", "has_finding"),
    ];

    const result = groupServicesUnderAssets(nodes, edges);

    expect(result.nodes).toHaveLength(2);
    const findingEdge = result.edges.find((e) => e.id === "e-finding");
    expect(findingEdge).toBeDefined();
    expect(findingEdge!.target).toBe("asset-1");
    expect(findingEdge!.metadata.original_target).toBe("svc-1");
  });

  it("returns unchanged input when graph has no exposes edges", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("svc-1", "service"),
    ];
    const edges = [makeEdge("e1", "asset-1", "svc-1", "related")];

    const result = groupServicesUnderAssets(nodes, edges);

    expect(result.nodes).toBe(nodes);
    expect(result.edges).toBe(edges);
  });

  it("does not absorb network-group members into wrong groups", () => {
    const nodes = [
      makeNode("net-1", "network"),
      makeNode("asset-1", "asset"),
      makeNode("svc-1", "service"),
    ];
    const edges = [
      makeEdge("e-contain", "net-1", "asset-1", "contains"),
      makeEdge("e-expose", "asset-1", "svc-1", "exposes"),
    ];

    const result = groupServicesUnderAssets(nodes, edges);

    expect(result.nodes).toHaveLength(2);
    expect(result.nodes.find((n) => n.id === "net-1")).toBeDefined();
    expect(result.nodes.find((n) => n.id === "asset-1")).toBeDefined();
    expect(result.nodes.find((n) => n.id === "svc-1")).toBeUndefined();
  });
});

describe("groupFindingsUnderAssets", () => {
  it("attaches asset-targeted has_finding edges to the asset", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("finding-1", "finding", {
        label: "Password exposure",
        metadata: { state: { severity: "medium" } },
      }),
    ];
    const edges = [makeEdge("e1", "asset-1", "finding-1", "has_finding")];

    const result = groupFindingsUnderAssets(nodes, edges);

    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("asset-1");
    expect(result.nodes[0].childFindings).toEqual([
      expect.objectContaining({
        id: "finding-1",
        label: "Password exposure",
        severity: "medium",
      }),
    ]);
    expect(result.edges).toHaveLength(0);
  });

  it("attaches service-targeted findings to the parent asset after service absorption", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("svc-1", "service", { metadata: { port: 22, protocol: "tcp" } }),
      makeNode("finding-1", "finding", {
        label: "Weak SSH authentication",
        metadata: { severity: "high" },
      }),
    ];
    const serviceGrouped = groupServicesUnderAssets(nodes, [
      makeEdge("e-expose", "asset-1", "svc-1", "exposes"),
      makeEdge("e-finding", "svc-1", "finding-1", "has_finding"),
    ]);

    const result = groupFindingsUnderAssets(serviceGrouped.nodes, serviceGrouped.edges);

    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("asset-1");
    expect(result.nodes[0].childServices).toHaveLength(1);
    expect(result.nodes[0].childFindings[0]).toEqual(
      expect.objectContaining({
        id: "finding-1",
        severity: "high",
      }),
    );
    expect(result.edges).toHaveLength(0);
  });

  it("keeps orphan findings as standalone nodes", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("finding-orphan", "finding"),
    ];
    const edges = [makeEdge("e1", "asset-1", "finding-orphan", "related")];

    const result = groupFindingsUnderAssets(nodes, edges);

    expect(result.nodes).toHaveLength(2);
    expect(result.nodes.find((node) => node.id === "finding-orphan")).toBeDefined();
    expect(result.edges).toHaveLength(1);
  });

  it("sorts multiple asset findings by severity and label", () => {
    const nodes = [
      makeNode("asset-1", "asset"),
      makeNode("finding-low", "finding", {
        label: "Z low",
        metadata: { severity: "low" },
      }),
      makeNode("finding-high-b", "finding", {
        label: "B high",
        metadata: { severity: "high" },
      }),
      makeNode("finding-high-a", "finding", {
        label: "A high",
        metadata: { severity: "high" },
      }),
    ];
    const edges = [
      makeEdge("e1", "asset-1", "finding-low", "has_finding"),
      makeEdge("e2", "asset-1", "finding-high-b", "has_finding"),
      makeEdge("e3", "asset-1", "finding-high-a", "has_finding"),
    ];

    const result = groupFindingsUnderAssets(nodes, edges);

    expect(result.nodes[0].childFindings.map((finding) => finding.id)).toEqual([
      "finding-high-a",
      "finding-high-b",
      "finding-low",
    ]);
  });
});
