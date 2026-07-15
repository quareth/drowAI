// @vitest-environment jsdom
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EngagementFindingDetailPanel } from "@/components/engagements/engagement-finding-detail-panel";
import { EngagementFindingsTable } from "@/components/engagements/engagement-findings-table";
import { AuthContext } from "@/hooks/use-auth";
import type { FindingDetail, FindingListItem, FindingsFilters } from "@/types/engagement-knowledge";

const listRows: FindingListItem[] = [
  {
    id: "f-1",
    engagement_id: 11,
    finding_key: "finding.one",
    finding_type: "finding.vulnerability",
    subject_type: "host.ip",
    subject_key: "host.ip:10.0.0.10",
    asset_id: "asset-1",
    service_id: "service-1",
    title: "OpenSSL CVE",
    severity: "critical",
    status: "open",
    assertion_level: "observed",
    confidence: "high",
    first_seen_at: "2026-03-08T08:00:00Z",
    last_seen_at: "2026-03-08T08:15:00Z",
    is_exploited: false,
    is_open: true,
    source_tool: "nmap",
    asset: {
      id: "asset-1",
      asset_key: "host.ip:10.0.0.10",
      asset_type: "host.ip",
      display_name: "10.0.0.10",
      ip_address: "10.0.0.10",
      hostname: null,
      status: "up",
      last_seen_at: "2026-03-08T08:15:00Z",
    },
    service: {
      id: "service-1",
      service_key: "service.socket:10.0.0.10/tcp/443",
      asset_id: "asset-1",
      protocol: "tcp",
      port: 443,
      service_name: "https",
      product: "nginx",
      version: "1.25",
      status: "open",
      last_seen_at: "2026-03-08T08:14:00Z",
    },
    evidence_count: 2,
    affected_asset_count: 3,
    evidence_refs: [{ evidence_archive_id: "ev-1" }, { evidence_archive_id: "ev-2" }],
  },
  {
    id: "f-2",
    engagement_id: 11,
    finding_key: "finding.two",
    finding_type: "finding.vulnerability",
    subject_type: "host.ip",
    subject_key: "host.ip:10.0.0.11",
    asset_id: "asset-2",
    service_id: null,
    title: "Exploited service",
    severity: "high",
    status: "open",
    assertion_level: "exploited",
    confidence: "high",
    first_seen_at: "2026-03-08T07:00:00Z",
    last_seen_at: "2026-03-08T08:20:00Z",
    is_exploited: true,
    is_open: true,
    source_tool: "metasploit",
    asset: null,
    service: null,
    evidence_count: 1,
    affected_asset_count: 1,
    evidence_refs: [{ evidence_archive_id: "ev-9" }],
  },
];

const selectedDetail: FindingDetail = {
  ...listRows[0],
  asset: {
    id: "asset-1",
    asset_key: "host.ip:10.0.0.10",
    asset_type: "host.ip",
    display_name: "10.0.0.10",
    ip_address: "10.0.0.10",
    hostname: null,
    status: "up",
    last_seen_at: "2026-03-08T08:15:00Z",
  },
  service: {
    id: "service-1",
    service_key: "service.socket:10.0.0.10/tcp/443",
    asset_id: "asset-1",
    protocol: "tcp",
    port: 443,
    service_name: "https",
    product: "nginx",
    version: "1.25",
    status: "open",
    last_seen_at: "2026-03-08T08:14:00Z",
  },
  evidence_summary: { evidence_refs: [{ evidence_archive_id: "ev-1" }] },
  metadata: {
    source_tool: "nmap",
    state: {
      detector_id: "nmap/ssl-cert-expired",
      script_id: "ssl-cert",
      summary: "Subject: CN=example.com; Not valid after: 2025-01-01T00:00:00 - expired",
    },
  },
};

afterEach(() => {
  cleanup();
});

function renderWithProviders(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <AuthContext.Provider
          value={{
            user: null,
            isLoading: false,
            error: null,
            loginMutation: {} as never,
            logoutMutation: {} as never,
            registerMutation: {} as never,
          }}
        >
          {children}
        </AuthContext.Provider>
      </QueryClientProvider>
    );
  }

  return render(ui, { wrapper: Wrapper });
}

function selectRadixOption(label: string, optionText: string) {
  fireEvent.keyDown(screen.getByLabelText(label), {
    key: "ArrowDown",
    code: "ArrowDown",
  });
  const option = screen
    .getAllByText(optionText)
    .map((element) => element.closest('[role="option"]') || element)
    .find((element) => element.getAttribute("role") === "option");
  expect(option).toBeTruthy();
  fireEvent.click(option!);
}

describe("engagement-findings-table", () => {
  it("updates filters and selects rows", () => {
    const onFiltersChange = vi.fn();
    const onSelectFinding = vi.fn();
    const filters: FindingsFilters = { limit: 50, offset: 0 };

    renderWithProviders(
      <EngagementFindingsTable
        findings={listRows}
        filters={filters}
        onFiltersChange={onFiltersChange}
        selectedFindingId={null}
        onSelectFinding={onSelectFinding}
      />,
    );

    selectRadixOption("Severity Filter", "Critical");
    expect(onFiltersChange).toHaveBeenCalledWith(
      expect.objectContaining({ severity: "critical" }),
    );

    fireEvent.change(screen.getByLabelText("Search Findings"), {
      target: { value: "openssl" },
    });
    expect(onFiltersChange).toHaveBeenCalledWith(
      expect.objectContaining({ query: "openssl" }),
    );

    fireEvent.click(screen.getByText("OpenSSL CVE"));
    expect(onSelectFinding).toHaveBeenCalledWith("f-1");
    expect(screen.getByLabelText("severity-critical").className).toContain("border-red");
    expect(screen.getByLabelText("severity-high").className).toContain("border-orange");
    expect(screen.getByText("Critical")).toBeTruthy();
    expect(screen.getByText("High")).toBeTruthy();
    expect(screen.getByLabelText("finding-status-open").textContent).toBe("Open");
    expect(screen.getByLabelText("finding-status-exploited").textContent).toBe("Exploited");
    expect(screen.getByLabelText("finding-status-exploited").className).toContain("border-slate");
    expect(screen.queryByText("Not exploited")).toBeNull();
  });

  it("renders deterministic source and evidence cells", () => {
    renderWithProviders(
      <EngagementFindingsTable
        findings={listRows}
        filters={{}}
        onFiltersChange={vi.fn()}
        selectedFindingId="f-2"
        onSelectFinding={vi.fn()}
      />,
    );

    expect(screen.getAllByText("nmap").length).toBeGreaterThan(0);
    expect(screen.getByText("metasploit")).toBeTruthy();
    expect(screen.getByText("10.0.0.10")).toBeTruthy();
    expect(screen.queryByText("10.0.0.10 / HTTPS TCP 443")).toBeNull();
    expect(screen.queryByText("asset-1 / service-1")).toBeNull();
    expect(screen.getByText("3 assets")).toBeTruthy();
    expect(screen.getAllByText("2").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Exploited").length).toBeGreaterThan(0);
  });

  it("keeps raw backend filter values when selecting status filters", () => {
    const onFiltersChange = vi.fn();

    renderWithProviders(
      <EngagementFindingsTable
        findings={listRows}
        filters={{}}
        onFiltersChange={onFiltersChange}
        selectedFindingId={null}
        onSelectFinding={vi.fn()}
      />,
    );

    selectRadixOption("Status Filter", "Exploited");
    expect(onFiltersChange).toHaveBeenCalledWith(
      expect.objectContaining({ status: "exploited" }),
    );
  });

  it("uses compact columns so the split-pane table does not obscure details", () => {
    const { container } = renderWithProviders(
      <EngagementFindingsTable
        findings={listRows}
        filters={{}}
        onFiltersChange={vi.fn()}
        selectedFindingId={null}
        onSelectFinding={vi.fn()}
      />,
    );

    expect(container.querySelector("table")?.className).toContain("table-fixed");
    expect(container.querySelector("table")?.className).toContain("min-w-[700px]");
    expect(screen.getByText("Affected Asset").className).toContain("hidden");
    expect(screen.getByText("Last Observed").className).toContain("hidden");
    expect(screen.queryByText("Exploit State")).toBeNull();
    expect(screen.getByText("OpenSSL CVE").className).toContain("truncate");
  });

  it("renders custom empty message and loading skeleton states", () => {
    const { rerender } = renderWithProviders(
      <EngagementFindingsTable
        findings={[]}
        filters={{}}
        onFiltersChange={vi.fn()}
        selectedFindingId={null}
        onSelectFinding={vi.fn()}
        emptyMessage="No durable findings have been projected for this engagement yet."
      />,
    );
    expect(
      screen.getByText("No durable findings have been projected for this engagement yet."),
    ).toBeTruthy();

    rerender(
      <EngagementFindingsTable
        findings={[]}
        filters={{}}
        onFiltersChange={vi.fn()}
        selectedFindingId={null}
        onSelectFinding={vi.fn()}
        isLoading
      />,
    );
    expect(screen.getByLabelText("findings-loading-skeleton")).toBeTruthy();
  });
});

describe("engagement-finding-detail-panel", () => {
  it("renders selected finding detail and evidence preview rows", () => {
    const onPreviewEvidence = vi.fn();
    const onOpenAsset = vi.fn();
    render(
      <EngagementFindingDetailPanel
        finding={selectedDetail}
        onPreviewEvidence={onPreviewEvidence}
        onOpenAsset={onOpenAsset}
      />,
    );

    expect(screen.getAllByText("OpenSSL CVE").length).toBeGreaterThan(0);
    expect(screen.getByText("Critical").className).toContain("border-red");
    expect(screen.getByLabelText("finding-status-open").textContent).toBe("Open");
    expect(screen.queryByText("Not exploited")).toBeNull();
    expect(screen.getByText("source: nmap")).toBeTruthy();
    expect(screen.getByText("Linked Asset")).toBeTruthy();
    expect(screen.getByText("10.0.0.10")).toBeTruthy();
    expect(screen.getByText("Linked Service")).toBeTruthy();
    expect(screen.getByText("HTTPS")).toBeTruthy();
    expect(screen.getByText("service.socket:10.0.0.10/tcp/443")).toBeTruthy();
    expect(screen.getByText("Detection Rule")).toBeTruthy();
    expect(screen.getByText("Rule: nmap/ssl-cert-expired")).toBeTruthy();
    expect(screen.getByText("Script: ssl-cert")).toBeTruthy();
    expect(screen.getByText("ev-1")).toBeTruthy();
    expect(screen.getByText("Provenance Lineage")).toBeTruthy();
    expect(screen.getByText("Finding Key: finding.one")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Preview" })[0]);
    expect(onPreviewEvidence).toHaveBeenCalledWith("ev-1");
    fireEvent.click(screen.getByRole("button", { name: "Open in Assets" }));
    expect(onOpenAsset).toHaveBeenCalledWith("asset-1");
  });

  it("shows exploited status when projection marks a finding exploited independently from raw status", () => {
    render(
      <EngagementFindingDetailPanel
        finding={{
          ...selectedDetail,
          assertion_level: "exploited",
          is_exploited: true,
          status: "open",
        }}
      />,
    );

    expect(screen.getByLabelText("finding-status-exploited").textContent).toBe("Exploited");
  });
});
