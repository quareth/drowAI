// @vitest-environment jsdom
/* Verifies the threat dashboard renders analyst insights from engagement knowledge contracts. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThreatDashboardPanel } from "@/components/panels/threat-dashboard-panel";

const refreshMock = vi.fn();

vi.mock("@/hooks/use-user-timezone", () => ({
  useUserTimezone: () => "UTC",
}));

vi.mock("@/hooks/useTaskManagement", () => ({
  useTaskManagement: () => ({
    isLoading: false,
    tasks: [
      {
        id: 11,
        user_id: 7,
        engagement_id: 1,
        engagement_name: "MVP Perimeter",
        name: "External perimeter sweep",
        status: "running",
        created_at: "2026-06-13T08:00:00Z",
        updated_at: "2026-06-13T09:00:00Z",
      },
    ],
  }),
}));

vi.mock("@/hooks/use-engagement-knowledge", () => ({
  useEngagements: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: 1,
          user_id: 7,
          name: "MVP Perimeter",
          description: null,
          status: "active",
          metadata: {},
          created_at: "2026-06-13T07:00:00Z",
          updated_at: "2026-06-13T09:00:00Z",
        },
      ],
      total: 1,
      limit: 25,
      offset: 0,
    },
  }),
  useEngagementSummary: () => ({
    isLoading: false,
    data: {
      engagement_id: 1,
      open_findings_total: 3,
      open_findings_by_severity: {
        critical: 1,
        high: 1,
        medium: 1,
        low: 0,
        info: 0,
      },
      asset_counts: {
        total: 8,
        vulnerable: 2,
        exploited: 1,
      },
      service_count: 14,
      evidence_count: 9,
      relationship_count: 6,
      last_observed_at: "2026-06-13T09:15:00Z",
      open_statuses: ["open", "confirmed"],
    },
  }),
  useEngagementFindings: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "finding-1",
          engagement_id: 1,
          finding_key: "nuclei:cve-2024-demo",
          finding_type: "vulnerability",
          subject_type: "service",
          subject_key: "https://app.example",
          asset_id: "asset-1",
          service_id: "service-1",
          title: "Exposed admin console",
          severity: "critical",
          status: "open",
          assertion_level: "observed",
          confidence: "high",
          first_seen_at: "2026-06-13T08:20:00Z",
          last_seen_at: "2026-06-13T09:10:00Z",
          is_exploited: true,
          is_open: true,
          source_tool: "scanners.nuclei",
          asset: {
            id: "asset-1",
            asset_key: "host:10.0.0.5",
            asset_type: "host.ip",
            display_name: "10.0.0.5",
            ip_address: "10.0.0.5",
            hostname: null,
            status: "observed",
            last_seen_at: "2026-06-13T09:10:00Z",
          },
          service: {
            id: "service-1",
            service_key: "service.socket:10.0.0.5/tcp/443",
            asset_id: "asset-1",
            protocol: "tcp",
            port: 443,
            service_name: "https",
            product: null,
            version: null,
            status: "open",
            last_seen_at: "2026-06-13T09:10:00Z",
            metadata: {},
          },
          evidence_count: 4,
          affected_asset_count: 1,
          evidence_refs: [],
        },
      ],
      total: 1,
      limit: 6,
      offset: 0,
    },
  }),
  useEngagementAssets: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "asset-1",
          engagement_id: 1,
          asset_key: "host:10.0.0.5",
          asset_type: "host.ip",
          display_name: "10.0.0.5",
          ip_address: "10.0.0.5",
          hostname: null,
          status: "observed",
          first_seen_at: "2026-06-13T08:00:00Z",
          last_seen_at: "2026-06-13T09:10:00Z",
          max_confidence: "high",
          metadata: {},
          finding_count: 2,
          is_vulnerable: true,
          is_exploited: true,
          service_count: 3,
        },
      ],
      total: 1,
      limit: 5,
      offset: 0,
    },
  }),
  useEngagementEvidence: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "evidence-1",
          engagement_id: 1,
          task_id: 11,
          source_execution_id: "exec-1",
          source_artifact_id: "artifact-1",
          storage_mode: "object",
          content_sha256: "abc",
          byte_size: 42,
          mime_type: "application/json",
          source_tool: "scanners.nuclei",
          evidence_type: "finding",
          lineage: {},
          metadata: {},
          created_at: "2026-06-13T09:10:00Z",
        },
      ],
      total: 1,
      limit: 5,
      offset: 0,
    },
  }),
  useEngagementKnowledgeRefresh: () => ({
    refresh: refreshMock,
    isRefreshing: false,
  }),
}));

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  refreshMock.mockClear();
});

describe("ThreatDashboardPanel", () => {
  it("renders dashboard widgets from engagement knowledge instead of placeholder threat APIs", () => {
    render(<ThreatDashboardPanel />);

    expect(screen.getByText("MVP Perimeter")).toBeTruthy();
    expect(screen.getAllByText("Critical").length).toBeGreaterThan(0);
    expect(screen.getByText("Exposed admin console")).toBeTruthy();
    expect(screen.getByText(/10\.0\.0\.5 · nuclei ·/)).toBeTruthy();
    expect(screen.getByText("10.0.0.5")).toBeTruthy();
    expect(screen.getByText("External perimeter sweep")).toBeTruthy();
    expect(screen.queryByText("Recent Evidence")).toBeNull();
    expect(screen.queryByText("Blocked Threats")).toBeNull();
    expect(screen.queryByText("Firewall Status")).toBeNull();
  });
});
