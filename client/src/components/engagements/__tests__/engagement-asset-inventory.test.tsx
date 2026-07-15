// @vitest-environment jsdom
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EngagementAssetDetailPanel } from "@/components/engagements/engagement-asset-detail-panel";
import { EngagementAssetInventory } from "@/components/engagements/engagement-asset-inventory";
import { AuthContext } from "@/hooks/use-auth";
import type { AssetDetail, AssetListItem, AssetsFilters } from "@/types/engagement-knowledge";

const assetRows: AssetListItem[] = [
  {
    id: "asset-1",
    engagement_id: 9,
    asset_key: "host.ip:10.0.0.10",
    asset_type: "host.ip",
    display_name: "10.0.0.10",
    ip_address: "10.0.0.10",
    hostname: null,
    status: "up",
    first_seen_at: "2026-03-08T07:00:00Z",
    last_seen_at: "2026-03-08T08:10:00Z",
    max_confidence: "high",
    metadata: {},
    finding_count: 2,
    is_vulnerable: true,
    is_exploited: true,
    service_count: 1,
  },
  {
    id: "asset-2",
    engagement_id: 9,
    asset_key: "host.ip:10.0.0.11",
    asset_type: "host.ip",
    display_name: "10.0.0.11",
    ip_address: "10.0.0.11",
    hostname: null,
    status: "up",
    first_seen_at: "2026-03-08T06:00:00Z",
    last_seen_at: "2026-03-08T08:05:00Z",
    max_confidence: "high",
    metadata: {},
    finding_count: 1,
    is_vulnerable: true,
    is_exploited: false,
    service_count: 2,
  },
];

const assetDetail: AssetDetail = {
  ...assetRows[0],
  services: [
    {
      id: "service-1",
      service_key: "service.socket:10.0.0.10/tcp/443",
      asset_id: "asset-1",
      protocol: "tcp",
      port: 443,
      service_name: "https",
      product: "nginx",
      version: "1.25",
      status: "open",
      last_seen_at: "2026-03-08T08:00:00Z",
    },
  ],
  findings: [
    {
      id: "finding-1",
      engagement_id: 9,
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
      first_seen_at: "2026-03-08T07:30:00Z",
      last_seen_at: "2026-03-08T08:10:00Z",
      is_exploited: false,
      is_open: true,
      source_tool: "nmap",
      evidence_count: 2,
      evidence_refs: [{ evidence_archive_id: "ev-1" }, { evidence_archive_id: "ev-2" }],
    },
  ],
};

afterEach(() => {
  cleanup();
});

function renderWithAuth(ui: ReactNode) {
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
  fireEvent.click(screen.getByText(optionText));
}

describe("engagement-asset-inventory", () => {
  it("updates filters and selects assets", () => {
    const onFiltersChange = vi.fn();
    const onSelectAsset = vi.fn();
    const filters: AssetsFilters = { limit: 50, offset: 0 };

    renderWithAuth(
      <EngagementAssetInventory
        assets={assetRows}
        filters={filters}
        onFiltersChange={onFiltersChange}
        selectedAssetId={null}
        onSelectAsset={onSelectAsset}
      />,
    );

    selectRadixOption("Asset Type Filter", "Host IP");
    expect(onFiltersChange).toHaveBeenCalledWith(
      expect.objectContaining({ type: "host.ip" }),
    );

    fireEvent.change(screen.getByLabelText("Asset Search"), {
      target: { value: "10.0.0.10" },
    });
    expect(onFiltersChange).toHaveBeenCalledWith(
      expect.objectContaining({ query: "10.0.0.10" }),
    );

    fireEvent.click(screen.getByText("10.0.0.10"));
    expect(onSelectAsset).toHaveBeenCalledWith("asset-1");
    expect(screen.getAllByText("Vulnerable")[0].className).toContain("border-slate");
    expect(screen.getByText("Yes").className).toContain("border-slate");
    expect(screen.getByText("No").className).toContain("border-slate");
    expect(screen.getAllByText("2")[0].className).toContain("border-slate");
  });

  it("renders custom empty message and loading skeleton", () => {
    const { rerender } = renderWithAuth(
      <EngagementAssetInventory
        assets={[]}
        filters={{}}
        onFiltersChange={vi.fn()}
        selectedAssetId={null}
        onSelectAsset={vi.fn()}
        emptyMessage="No durable assets are available for this engagement yet."
      />,
    );
    expect(screen.getByText("No durable assets are available for this engagement yet.")).toBeTruthy();

    rerender(
      <EngagementAssetInventory
        assets={[]}
        filters={{}}
        onFiltersChange={vi.fn()}
        selectedAssetId={null}
        onSelectAsset={vi.fn()}
        isLoading
      />,
    );
    expect(screen.getByLabelText("assets-loading-skeleton")).toBeTruthy();
  });
});

describe("engagement-asset-detail-panel", () => {
  it("renders linked services and findings", () => {
    const onPreviewEvidence = vi.fn();
    render(<EngagementAssetDetailPanel asset={assetDetail} onPreviewEvidence={onPreviewEvidence} />);

    expect(screen.getAllByText("10.0.0.10").length).toBeGreaterThan(0);
    expect(screen.getByText("Risk State")).toBeTruthy();
    expect(screen.getByText("Vulnerable: Yes")).toBeTruthy();
    expect(screen.getByText("Exploited: Yes")).toBeTruthy();
    expect(screen.getByText("Linked Services")).toBeTruthy();
    expect(screen.getByText("HTTPS")).toBeTruthy();
    expect(screen.getByText("service.socket:10.0.0.10/tcp/443")).toBeTruthy();
    expect(screen.getByText("Linked Findings")).toBeTruthy();
    expect(screen.getByText("OpenSSL CVE")).toBeTruthy();
    expect(screen.getByText("evidence: 2")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(onPreviewEvidence).toHaveBeenCalledWith("ev-1");
  });
});
