/* Tests compact Territory asset node rendering for attached services and findings. */

// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AssetNode } from "@/components/engagements/territory/asset-node";
import { getTopologyNodeDimensions } from "@/components/engagements/territory/topology-presentation";
import type { TopologyNode } from "@/components/engagements/territory/topology-types";

vi.mock("@xyflow/react", () => ({
  Handle: () => <span data-testid="topology-handle" />,
  Position: { Left: "left", Right: "right" },
}));

afterEach(() => {
  cleanup();
});

function makeAssetNode(): TopologyNode {
  return {
    id: "asset-1",
    label: "10.10.10.5",
    kind: "asset",
    metadata: { is_vulnerable: true },
    networkId: "network:10.10.10.0/24",
    sourceNode: null,
    synthetic: false,
    childServices: [
      {
        id: "svc-22",
        label: "SSH",
        port: 22,
        protocol: "tcp",
        status: "open",
        sourceNode: null,
        metadata: {},
      },
      {
        id: "svc-80",
        label: "HTTP",
        port: 80,
        protocol: "tcp",
        status: "open",
        sourceNode: null,
        metadata: {},
      },
      {
        id: "svc-443",
        label: "HTTPS",
        port: 443,
        protocol: "tcp",
        status: "open",
        sourceNode: null,
        metadata: {},
      },
      {
        id: "svc-8080",
        label: "HTTP Alt",
        port: 8080,
        protocol: "tcp",
        status: "open",
        sourceNode: null,
        metadata: {},
      },
    ],
    childFindings: [
      {
        id: "finding-high",
        label: "Credential material exposed in packet capture",
        severity: "high",
        status: "open",
        sourceNode: null,
        metadata: {},
      },
      {
        id: "finding-medium",
        label: "Weak TLS",
        severity: "medium",
        status: "open",
        sourceNode: null,
        metadata: {},
      },
      {
        id: "finding-low",
        label: "Informational banner",
        severity: "low",
        status: "open",
        sourceNode: null,
        metadata: {},
      },
    ],
  };
}

describe("AssetNode", () => {
  it("renders compact service overflow and attached finding badges", () => {
    const topologyNode = makeAssetNode();
    const props = {
      selected: false,
      data: {
        node: topologyNode,
        memberCount: 0,
        isCollapsed: false,
      },
    } as any;

    render(
      <AssetNode {...props} />,
    );

    expect(screen.getByText("10.10.10.5")).toBeTruthy();
    expect(screen.getByText("4 svc")).toBeTruthy();
    expect(screen.getByText("3 findings")).toBeTruthy();
    expect(screen.getByTestId("finding-badge-finding-high").textContent).toContain(
      "High: Credential exposure",
    );
    expect(screen.getByTestId("finding-badge-finding-high").getAttribute("title")).toBe(
      "High: Credential material exposed in packet capture",
    );
    expect(screen.queryByTestId("finding-badge-finding-medium")).toBeNull();
    expect(screen.queryByTestId("finding-badge-finding-low")).toBeNull();
    expect(screen.getByTestId("service-chip-svc-22")).toBeTruthy();
    expect(screen.getByTestId("service-chip-svc-80")).toBeTruthy();
    expect(screen.queryByTestId("service-chip-svc-443")).toBeNull();
    expect(screen.queryByTestId("service-chip-svc-8080")).toBeNull();
    expect(screen.getByRole("button", { name: "Show all findings for asset" }).textContent).toBe("+2");
    expect(screen.getByRole("button", { name: "Show all services for asset" }).textContent).toBe("+2");
    const dimensions = getTopologyNodeDimensions(topologyNode);
    const renderedNode = screen.getByTestId("territory-node-asset-1");
    expect(renderedNode.style.height).toBe(`${dimensions.height}px`);
    expect(renderedNode.style.width).toBe(`${dimensions.width}px`);
  });

  it("uses the smallest size tier for asset-only nodes", () => {
    const props = {
      selected: false,
      data: {
        node: {
          ...makeAssetNode(),
          metadata: {},
          childServices: [],
          childFindings: [],
        },
        memberCount: 0,
        isCollapsed: false,
      },
    } as any;

    render(<AssetNode {...props} />);

    const node = screen.getByTestId("territory-node-asset-1");
    expect(node.style.height).toBe("84px");
    expect(node.style.width).toBe("230px");
  });
});
