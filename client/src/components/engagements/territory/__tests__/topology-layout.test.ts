/* Tests worker-backed topology layout projection, fallbacks, and singleton prewarming. */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  TopologyEdge,
  TopologyLayoutInput,
  TopologyNode,
} from "@/components/engagements/territory/topology-types";

const elkMocks = vi.hoisted(() => {
  const layout = vi.fn();
  const workerConstructor = vi.fn(function MockWorker() {});
  const elkConstructor = vi.fn(function MockElk(options: {
    workerFactory: () => Worker;
  }) {
    options.workerFactory();
    return { layout };
  });

  return { elkConstructor, layout, workerConstructor };
});

vi.mock("elkjs/lib/elk-api.js", () => ({
  default: elkMocks.elkConstructor,
}));

vi.mock("elkjs/lib/elk-worker.min.js?worker", () => ({
  default: elkMocks.workerConstructor,
}));

function makeNode(id: string, kind: TopologyNode["kind"] = "asset"): TopologyNode {
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
  };
}

function makeEdge(id: string, source: string, target: string): TopologyEdge {
  return {
    id,
    source,
    target,
    kind: "route",
    label: null,
    metadata: {},
    sourceEdge: null,
    synthetic: false,
  };
}

function makeInput(nodeCount = 2): TopologyLayoutInput {
  const nodes = Array.from({ length: nodeCount }, (_, index) =>
    makeNode(`node-${index + 1}`, index === 0 ? "network" : "asset"),
  );
  return {
    nodes,
    edges: nodeCount > 1 ? [makeEdge("edge-1", "node-1", "node-2")] : [],
  };
}

beforeEach(() => {
  vi.resetModules();
  elkMocks.layout.mockReset();
  elkMocks.workerConstructor.mockClear();
  elkMocks.elkConstructor.mockClear();
});

describe("topology-layout", () => {
  it("projects ELK coordinates without changing the graph payload or options", async () => {
    elkMocks.layout.mockResolvedValue({
      children: [
        { id: "node-1", x: 31, y: 47 },
        { id: "node-2", x: 211 },
      ],
    });
    const { computeTopologyLayout } = await import(
      "@/components/engagements/territory/topology-layout"
    );

    await expect(computeTopologyLayout(makeInput())).resolves.toEqual({
      "node-1": { x: 31, y: 47 },
      "node-2": { x: 211, y: 0 },
    });
    expect(elkMocks.layout).toHaveBeenCalledWith({
      id: "territory-root",
      layoutOptions: {
        "elk.algorithm": "layered",
        "elk.direction": "RIGHT",
        "elk.layered.spacing.nodeNodeBetweenLayers": "100",
        "elk.spacing.nodeNode": "80",
        "elk.edgeRouting": "SPLINES",
        "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      },
      children: [
        { id: "node-1", width: 280, height: 72 },
        { id: "node-2", width: 230, height: 84 },
      ],
      edges: [{ id: "edge-1", sources: ["node-1"], targets: ["node-2"] }],
    });
  });

  it("uses the deterministic grid when ELK rejects layout", async () => {
    elkMocks.layout.mockRejectedValue(new Error("layout failed"));
    const { computeTopologyLayout } = await import(
      "@/components/engagements/territory/topology-layout"
    );

    await expect(computeTopologyLayout(makeInput(5))).resolves.toEqual({
      "node-1": { x: 0, y: 0 },
      "node-2": { x: 280, y: 0 },
      "node-3": { x: 560, y: 0 },
      "node-4": { x: 840, y: 0 },
      "node-5": { x: 0, y: 150 },
    });
  });

  it("uses the deterministic grid when worker construction fails", async () => {
    elkMocks.workerConstructor.mockImplementationOnce(function WorkerFailure() {
      throw new Error("worker unavailable");
    });
    const { computeTopologyLayout } = await import(
      "@/components/engagements/territory/topology-layout"
    );

    await expect(computeTopologyLayout(makeInput(2))).resolves.toEqual({
      "node-1": { x: 0, y: 0 },
      "node-2": { x: 280, y: 0 },
    });
    expect(elkMocks.layout).not.toHaveBeenCalled();
  });

  it("constructs only one engine when prewarmed repeatedly and then used", async () => {
    elkMocks.layout.mockResolvedValue({ children: [] });
    const { computeTopologyLayout, prewarmTopologyLayout } = await import(
      "@/components/engagements/territory/topology-layout"
    );

    prewarmTopologyLayout();
    prewarmTopologyLayout();
    await computeTopologyLayout(makeInput(1));

    expect(elkMocks.elkConstructor).toHaveBeenCalledTimes(1);
    expect(elkMocks.workerConstructor).toHaveBeenCalledTimes(1);
  });

  it("returns an empty layout without constructing the engine", async () => {
    const { computeTopologyLayout } = await import(
      "@/components/engagements/territory/topology-layout"
    );

    await expect(computeTopologyLayout({ nodes: [], edges: [] })).resolves.toEqual({});
    expect(elkMocks.elkConstructor).not.toHaveBeenCalled();
  });
});
