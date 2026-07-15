/* Tests shared Territory presentation contracts for node sizing and compact labels. */

import { describe, expect, it } from "vitest";

import {
  compactFindingLabel,
  assetAccentClass,
  getTopologyNodeDimensions,
  severityRank,
  severityBadgeClass,
  TOPOLOGY_CANVAS_HEIGHT,
} from "@/components/engagements/territory/topology-presentation";
import type { TopologyNode } from "@/components/engagements/territory/topology-types";

function makeAsset(overrides: Partial<TopologyNode> = {}): TopologyNode {
  return {
    id: "asset-1",
    label: "10.10.10.5",
    kind: "asset",
    metadata: {},
    networkId: null,
    sourceNode: null,
    synthetic: false,
    childServices: [],
    childFindings: [],
    ...overrides,
  };
}

describe("topology-presentation", () => {
  it("derives content-aware asset dimensions from summarized content", () => {
    expect(getTopologyNodeDimensions(makeAsset())).toEqual({ width: 230, height: 84 });
    expect(
      getTopologyNodeDimensions(makeAsset({ metadata: { is_vulnerable: true } })),
    ).toEqual({ width: 230, height: 84 });
    expect(
      getTopologyNodeDimensions(makeAsset({
        childFindings: [
          {
            id: "finding-1",
            label: "Credential material exposed in packet capture",
            severity: "medium",
            status: "open",
            sourceNode: null,
            metadata: {},
          },
        ],
      })),
    ).toEqual({ width: 270, height: 88 });
    expect(
      getTopologyNodeDimensions(makeAsset({
        childServices: [
          {
            id: "svc-1",
            label: "ssh",
            port: 22,
            protocol: "tcp",
            status: "open",
            sourceNode: null,
            metadata: {},
          },
        ],
      })),
    ).toEqual({ width: 270, height: 116 });
    expect(
      getTopologyNodeDimensions(makeAsset({
        metadata: { is_vulnerable: true },
        childFindings: [
          {
            id: "finding-1",
            label: "Credential material exposed in packet capture",
            severity: "medium",
            status: "open",
            sourceNode: null,
            metadata: {},
          },
        ],
        childServices: [
          {
            id: "svc-1",
            label: "ssh",
            port: 22,
            protocol: "tcp",
            status: "open",
            sourceNode: null,
            metadata: {},
          },
        ],
      })),
    ).toEqual({ width: 270, height: 172 });
  });

  it("keeps canvas height and compact finding labels centralized", () => {
    expect(TOPOLOGY_CANVAS_HEIGHT).toBe(640);
    expect(compactFindingLabel("Credential material exposed in packet capture")).toBe(
      "Credential exposure",
    );
    expect(compactFindingLabel("This is a very long finding title that should not fit")).toBe(
      "This is a very long findi...",
    );
  });

  it("orders severities deterministically through the shared severity contract", () => {
    expect(["unknown", "medium", "critical", "low", "high", "info"].sort(
      (left, right) => severityRank(left) - severityRank(right),
    )).toEqual(["critical", "high", "medium", "low", "info", "unknown"]);
  });

  it("normalizes severity before resolving badges and asset accents", () => {
    expect(severityBadgeClass(" HIGH ")).toContain("border-orange");
    expect(severityBadgeClass("")).toContain("border-slate");
    expect(assetAccentClass(" Medium ", false, false)).toBe("border-l-amber-400");
    expect(assetAccentClass(" HIGH ", false, false)).toBe("border-l-orange-400");
    expect(assetAccentClass(" HIGH ", false, true)).toBe("border-l-orange-400");
    expect(assetAccentClass("", true, true)).toBe("border-l-slate-500");
  });
});
