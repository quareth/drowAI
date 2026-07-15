/* Tests Territory edge rendering rules for reducing routine relationship label noise. */

// @vitest-environment jsdom
import type { ComponentType, ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { topologyEdgeTypes } from "@/components/engagements/territory/topology-edges";

vi.mock("@xyflow/react", () => ({
  BaseEdge: () => <svg data-testid="base-edge" />,
  EdgeLabelRenderer: ({ children }: { children: ReactNode }) => <>{children}</>,
  getBezierPath: () => ["M0,0 C10,10 20,20 30,30", 15, 15],
}));

afterEach(() => {
  cleanup();
});

const TopologyEdge = topologyEdgeTypes.topology as ComponentType<any>;

function renderEdge(kind: string, label: string) {
  render(
    <TopologyEdge
      id={`edge-${kind}`}
      sourceX={0}
      sourceY={0}
      sourcePosition="right"
      targetX={30}
      targetY={30}
      targetPosition="left"
      markerEnd="arrow"
      data={{
        edge: {
          id: `edge-${kind}`,
          source: "source",
          target: "target",
          kind,
          label,
          metadata: {},
          sourceEdge: null,
          synthetic: false,
        },
      }}
    />,
  );
}

describe("topology-edges", () => {
  it("hides routine contains labels", () => {
    renderEdge("contains", "contains");
    expect(screen.queryByText("contains")).toBeNull();
  });

  it("keeps labels for non-routine relationship edges", () => {
    renderEdge("route", "route");
    expect(screen.getByText("route")).toBeTruthy();
  });
});
