// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WebSurfacePanel } from "@/components/engagements/territory/web-surface-panel";
import type { GraphNode } from "@/types/engagement-knowledge";

const mockedHooks = vi.hoisted(() => ({
  useEngagementWebSurfaceOrigins: vi.fn(),
  useEngagementWebSurfacePathPage: vi.fn(),
}));

vi.mock("@/hooks/use-engagement-knowledge", () => ({
  useEngagementWebSurfaceOrigins: mockedHooks.useEngagementWebSurfaceOrigins,
  useEngagementWebSurfacePathPage: mockedHooks.useEngagementWebSurfacePathPage,
}));

const baseOriginsResult = {
  data: { service_key: "service.socket:10.0.0.10/tcp/443", items: [] },
  isLoading: false,
  isError: false,
};

const basePathsResult = {
  data: {
    service_key: "service.socket:10.0.0.10/tcp/443",
    origin_key: null,
    items: [],
    total: 0,
    limit: 100,
    offset: 0,
    hidden_noisy: 0,
  },
  isLoading: false,
  isError: false,
};

const httpServiceNode: GraphNode = {
  id: "service.socket:10.0.0.10/tcp/443",
  subject_key: "service.socket:10.0.0.10/tcp/443",
  node_type: "service",
  label: "https 443",
  metadata: { service_name: "https", transport_protocol: "tcp" },
};

beforeEach(() => {
  mockedHooks.useEngagementWebSurfaceOrigins.mockReset();
  mockedHooks.useEngagementWebSurfacePathPage.mockReset();
  mockedHooks.useEngagementWebSurfaceOrigins.mockReturnValue(baseOriginsResult);
  mockedHooks.useEngagementWebSurfacePathPage.mockReturnValue(basePathsResult);
});

afterEach(() => {
  cleanup();
});

describe("web-surface-panel", () => {
  it("hides for non-service selections", () => {
    const nonServiceNode: GraphNode = {
      id: "host.ip:10.0.0.10",
      subject_key: "host.ip:10.0.0.10",
      node_type: "asset",
      label: "10.0.0.10",
      metadata: {},
    };

    render(<WebSurfacePanel engagementId="7" selectedNode={nonServiceNode} />);

    expect(screen.queryByTestId("web-surface-panel")).toBeNull();
  });

  it("hides for non-http service selections", () => {
    const nonHttpServiceNode: GraphNode = {
      ...httpServiceNode,
      id: "service.socket:10.0.0.10/tcp/22",
      subject_key: "service.socket:10.0.0.10/tcp/22",
      metadata: { service_name: "ssh", transport_protocol: "tcp" },
    };

    render(<WebSurfacePanel engagementId="7" selectedNode={nonHttpServiceNode} />);

    expect(screen.queryByTestId("web-surface-panel")).toBeNull();
  });

  it("does not treat transport protocol alone as web surface metadata", () => {
    const transportOnlyServiceNode: GraphNode = {
      ...httpServiceNode,
      metadata: { transport_protocol: "tcp" },
    };

    render(<WebSurfacePanel engagementId="7" selectedNode={transportOnlyServiceNode} />);

    expect(screen.queryByTestId("web-surface-panel")).toBeNull();
  });

  it("renders collapsed summary counts and producers", () => {
    mockedHooks.useEngagementWebSurfaceOrigins.mockReturnValue({
      ...baseOriginsResult,
      data: {
        service_key: "service.socket:10.0.0.10/tcp/443",
        items: [
          {
            origin_key: "https://example.com",
            total_paths: 5,
            visible_paths: 4,
            hidden_noisy: 1,
            calibrated_warnings: 2,
            producers: ["ffuf", "gobuster"],
            first_seen_at: null,
            last_seen_at: null,
          },
        ],
      },
    });

    render(<WebSurfacePanel engagementId="7" selectedNode={httpServiceNode} />);

    expect(screen.getByTestId("web-surface-summary")).toBeTruthy();
    expect(screen.getByText("Origins: 1")).toBeTruthy();
    expect(screen.getByText("Visible paths: 4")).toBeTruthy();
    expect(screen.getByText("Total paths: 5")).toBeTruthy();
    expect(screen.getByText("Calibrated warnings: 2")).toBeTruthy();
    expect(screen.getByText("Hidden noisy paths: 1")).toBeTruthy();
    expect(screen.getByText("ffuf")).toBeTruthy();
    expect(screen.getByText("gobuster")).toBeTruthy();
  });

  it("expands one origin and renders path rows with producer badges and calibrated state", async () => {
    mockedHooks.useEngagementWebSurfaceOrigins.mockReturnValue({
      ...baseOriginsResult,
      data: {
        service_key: "service.socket:10.0.0.10/tcp/443",
        items: [
          {
            origin_key: "https://example.com",
            total_paths: 1,
            visible_paths: 1,
            hidden_noisy: 0,
            calibrated_warnings: 1,
            producers: ["ffuf"],
            first_seen_at: null,
            last_seen_at: null,
          },
        ],
      },
    });
    mockedHooks.useEngagementWebSurfacePathPage.mockReturnValue({
      ...basePathsResult,
      data: {
        ...basePathsResult.data,
        origin_key: "https://example.com",
        items: [
          {
            canonical_url: "https://example.com/admin",
            path: "/admin",
            last_status_code: 200,
            last_response_size: 1337,
            calibrated_baseline: true,
            noise_score: 0,
            producers: { ffuf: { seen_count: 1, last_seen_at: null, run_ids: [] } },
            first_seen_at: null,
            last_seen_at: null,
          },
        ],
      },
    });

    render(<WebSurfacePanel engagementId="7" selectedNode={httpServiceNode} />);
    fireEvent.click(screen.getByRole("button", { name: "Show Paths" }));

    await waitFor(() => {
      expect(screen.getByTestId("web-surface-paths")).toBeTruthy();
      expect(screen.getByText("/admin")).toBeTruthy();
      expect(screen.getAllByText("ffuf").length).toBeGreaterThan(0);
      expect(screen.getByText("Calibrated")).toBeTruthy();
    });
  });

  it("include-noisy toggle changes origin and path request params", async () => {
    mockedHooks.useEngagementWebSurfaceOrigins.mockReturnValue({
      ...baseOriginsResult,
      data: {
        service_key: "service.socket:10.0.0.10/tcp/443",
        items: [
          {
            origin_key: "https://example.com",
            total_paths: 1,
            visible_paths: 1,
            hidden_noisy: 0,
            calibrated_warnings: 0,
            producers: ["ffuf"],
            first_seen_at: null,
            last_seen_at: null,
          },
        ],
      },
    });

    render(<WebSurfacePanel engagementId="7" selectedNode={httpServiceNode} />);
    fireEvent.click(screen.getByRole("button", { name: "Show Paths" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Include noisy paths" }));

    await waitFor(() => {
      expect(mockedHooks.useEngagementWebSurfaceOrigins).toHaveBeenLastCalledWith(
        "7",
        "service.socket:10.0.0.10/tcp/443",
        { include_noisy: true },
      );
      expect(mockedHooks.useEngagementWebSurfacePathPage).toHaveBeenLastCalledWith(
        "7",
        "service.socket:10.0.0.10/tcp/443",
        { origin_key: "https://example.com", include_noisy: true, limit: 100, offset: 0 },
      );
    });
  });
});
