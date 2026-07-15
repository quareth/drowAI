// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EngagementMapPanel } from "@/components/engagements/engagement-map-panel";

afterEach(() => {
  cleanup();
});

describe("engagement-map-panel", () => {
  it("renders topology canvas controls for graph payload", async () => {
    render(
      <EngagementMapPanel
        graph={{
          engagement_id: 42,
          nodes: [
            {
              id: "n-1",
              subject_key: "host.ip:10.0.0.10",
              node_type: "asset",
              label: "10.0.0.10",
              metadata: { is_vulnerable: true },
            },
          ],
          edges: [
            {
              id: "e-1",
              source: "n-1",
              target: "service.socket:10.0.0.10/tcp/443",
              relationship_type: "exposes",
              confidence: "high",
              first_seen_at: null,
              last_seen_at: null,
              metadata: {},
            },
          ],
        }}
      />,
    );

    expect(
      screen.getByText("Territory topology preview: interactive, zoomable, read-only network map."),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Fit view" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Collapse all" })).toBeTruthy();
    expect(await screen.findByTestId("territory-topology-canvas")).toBeTruthy();
  });

  it("renders loading state", () => {
    render(<EngagementMapPanel isLoading />);
    expect(screen.getByText("Loading relationship map...")).toBeTruthy();
  });

  it("shows honest empty state when graph has no projected territory records", () => {
    render(
      <EngagementMapPanel
        graph={{
          engagement_id: 99,
          nodes: [],
          edges: [],
        }}
      />,
    );

    expect(
      screen.getByText("No durable territory graph data is available for this engagement yet."),
    ).toBeTruthy();
    expect(screen.queryByText(/Corp LAN|DMZ|Cloud Segment/)).toBeNull();
  });
});
