// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TopologyCanvas } from "@/components/engagements/territory/topology-canvas";
import type { TopologyGraph } from "@/components/engagements/territory/topology-types";

const fitViewSpy = vi.fn();

vi.mock("@xyflow/react", async () => {
  const React = await import("react");

  function MockReactFlow({
    nodes = [],
    edges = [],
    onNodeClick,
    onEdgeClick,
    children,
    ...props
  }: any) {
    return (
      <div data-testid={props["data-testid"] || "mock-reactflow"}>
        <div data-testid="mock-node-count">{nodes.length}</div>
        <div data-testid="mock-edge-count">{edges.length}</div>
        <button type="button" onClick={() => nodes[0] && onNodeClick?.({}, nodes[0])}>
          mock-select-node
        </button>
        <button type="button" onClick={() => edges[0] && onEdgeClick?.({}, edges[0])}>
          mock-select-edge
        </button>
        {children}
      </div>
    );
  }

  return {
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    useReactFlow: () => ({ fitView: fitViewSpy }),
    useNodesState: (initial: any[]) => {
      const [nodes, setNodes] = React.useState(initial);
      return [nodes, setNodes, vi.fn()];
    },
    useEdgesState: (initial: any[]) => {
      const [edges, setEdges] = React.useState(initial);
      return [edges, setEdges, vi.fn()];
    },
    MiniMap: (props: any) => <div data-testid={props["data-testid"] || "topology-minimap"} />,
    Controls: () => <div data-testid="topology-controls" />,
    Background: () => <div data-testid="topology-background" />,
    Handle: () => <span data-testid="topology-handle" />,
    Position: { Left: "left", Right: "right" },
    BaseEdge: () => <path />,
    EdgeLabelRenderer: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    getBezierPath: () => ["M0,0 C0,0 0,0 0,0", 0, 0],
    MarkerType: { ArrowClosed: "arrow-closed" },
  };
});

vi.mock("@/components/engagements/territory/topology-layout", () => ({
  computeTopologyLayout: vi.fn(async ({ nodes }: { nodes: { id: string }[] }) =>
    Object.fromEntries(nodes.map((node, index) => [node.id, { x: index * 120, y: 0 }])),
  ),
}));

const graph: TopologyGraph = {
  engagementId: 1,
  source: "graph",
  nodes: [
    {
      id: "network:corp",
      label: "Corp",
      kind: "network",
      metadata: {},
      networkId: null,
      sourceNode: {
        id: "network:corp",
        subject_key: "network.cidr:10.10.10.0/24",
        node_type: "network",
        label: "10.10.10.0/24",
        metadata: {},
      },
      synthetic: false,
      childServices: [],
      childFindings: [],
    },
    {
      id: "asset:1",
      label: "10.10.10.9",
      kind: "asset",
      metadata: {},
      networkId: "network:corp",
      sourceNode: {
        id: "asset:1",
        subject_key: "host.ip:10.10.10.9",
        node_type: "asset",
        label: "10.10.10.9",
        metadata: {},
      },
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
      sourceEdge: {
        id: "contains:1",
        source: "network:corp",
        target: "asset:1",
        relationship_type: "contains",
        confidence: "high",
        first_seen_at: null,
        last_seen_at: null,
        metadata: {},
      },
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

describe("topology-canvas", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fitViewSpy.mockReset();
    Object.defineProperty(window, "ResizeObserver", {
      writable: true,
      configurable: true,
      value: class ResizeObserver {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    });
  });

  afterEach(() => {
    cleanup();
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    // @ts-expect-error test cleanup for mocked browser API
    delete window.ResizeObserver;
  });

  it("renders controls/minimap and supports collapse/expand and selection callbacks", async () => {
    const onSelectNode = vi.fn();
    const onSelectEdge = vi.fn();

    render(
      <TopologyCanvas
        graph={graph}
        onSelectNode={onSelectNode}
        onSelectEdge={onSelectEdge}
      />,
    );

    await act(async () => {
      vi.advanceTimersByTime(160);
    });

    expect(screen.getByRole("button", { name: "Fit view" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Collapse all" })).toBeTruthy();
    expect(screen.getByTestId("territory-topology-minimap")).toBeTruthy();
    expect(screen.getByTestId("topology-controls")).toBeTruthy();
    expect(screen.getByTestId("mock-node-count").textContent).toBe("2");

    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    await act(async () => {
      vi.advanceTimersByTime(160);
    });
    expect(screen.getByTestId("mock-node-count").textContent).toBe("1");

    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    await act(async () => {
      vi.advanceTimersByTime(160);
    });
    expect(screen.getByTestId("mock-node-count").textContent).toBe("2");

    fireEvent.click(screen.getByRole("button", { name: "mock-select-node" }));
    fireEvent.click(screen.getByRole("button", { name: "mock-select-edge" }));
    expect(onSelectNode).toHaveBeenCalledTimes(1);
    expect(onSelectEdge).toHaveBeenCalledTimes(1);
  });
});
