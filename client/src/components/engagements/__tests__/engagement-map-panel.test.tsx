// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EngagementMapPanel } from "@/components/engagements/engagement-map-panel";

const mockedHooks = vi.hoisted(() => ({
  useEngagementWebSurfaceOrigins: vi.fn(() => ({
    data: { service_key: "service.socket:198.51.100.24/tcp/80", items: [] },
    isLoading: false,
    isError: false,
  })),
  useEngagementWebSurfacePathPage: vi.fn(() => ({
    data: {
      service_key: "service.socket:198.51.100.24/tcp/80",
      origin_key: null,
      items: [],
      total: 0,
      limit: 100,
      offset: 0,
      hidden_noisy: 0,
    },
    isLoading: false,
    isError: false,
  })),
}));

vi.mock("@/hooks/use-engagement-knowledge", () => mockedHooks);

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

  it("renders the generic web surface after selecting an HTTP service", () => {
    const serviceKey = "service.socket:198.51.100.24/tcp/80";
    render(
      <EngagementMapPanel
        graph={{
          engagement_id: 42,
          nodes: [
            {
              id: "host.ip:198.51.100.24",
              subject_key: "host.ip:198.51.100.24",
              node_type: "asset",
              label: "198.51.100.24",
              metadata: {},
            },
            {
              id: serviceKey,
              subject_key: serviceKey,
              node_type: "service",
              label: "HTTP",
              metadata: {
                service_name: "http",
                application_protocol: "http",
                protocol: "tcp",
                port: 80,
              },
            },
          ],
          edges: [
            {
              id: "exposes-http",
              source: "host.ip:198.51.100.24",
              target: serviceKey,
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

    const serviceChips = screen.getAllByTestId(`service-chip-${serviceKey}`);
    fireEvent.click(serviceChips[serviceChips.length - 1]);

    expect(screen.getByTestId("web-surface-panel")).toBeTruthy();
    expect(mockedHooks.useEngagementWebSurfaceOrigins).toHaveBeenLastCalledWith(
      42,
      serviceKey,
      { asset_key: "host.ip:198.51.100.24", include_noisy: false },
    );
  });
});
