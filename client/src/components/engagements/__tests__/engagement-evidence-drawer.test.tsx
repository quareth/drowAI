// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EngagementAssetDetailPanel } from "@/components/engagements/engagement-asset-detail-panel";
import { EngagementEvidenceDrawer } from "@/components/engagements/engagement-evidence-drawer";
import { EngagementFindingDetailPanel } from "@/components/engagements/engagement-finding-detail-panel";
import { AuthContext } from "@/hooks/use-auth";
import type { AssetDetail, EvidenceListItem, FindingDetail } from "@/types/engagement-knowledge";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

vi.mock("@/components/chat/tool-card-terminal/ToolCardTerminalOutput", () => ({
  ToolCardTerminalOutput: ({ outputText }: { outputText: string }) => (
    <pre data-testid="terminal-output">{outputText}</pre>
  ),
}));

const evidenceRows: EvidenceListItem[] = [
  {
    id: "ev-1",
    engagement_id: 1,
    task_id: 88,
    source_execution_id: "execution-abc12345",
    source_artifact_id: "artifact-1",
    storage_mode: "archived_file",
    content_sha256: null,
    byte_size: null,
    mime_type: null,
    source_tool: "nmap",
    evidence_type: "terminal",
    lineage: {},
    metadata: {},
    created_at: "2026-03-08T10:00:00Z",
  },
];

const findingDetail: FindingDetail = {
  id: "f-1",
  engagement_id: 1,
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
  first_seen_at: "2026-03-08T09:00:00Z",
  last_seen_at: "2026-03-08T09:10:00Z",
  is_exploited: false,
  is_open: true,
  source_tool: "nmap",
  evidence_count: 1,
  evidence_refs: [{ evidence_archive_id: "ev-1" }],
  asset: null,
  service: null,
  evidence_summary: {},
  metadata: {},
};

const assetDetail: AssetDetail = {
  id: "asset-1",
  engagement_id: 1,
  asset_key: "host.ip:10.0.0.10",
  asset_type: "host.ip",
  display_name: "10.0.0.10",
  ip_address: "10.0.0.10",
  hostname: null,
  status: "up",
  first_seen_at: "2026-03-08T08:00:00Z",
  last_seen_at: "2026-03-08T09:00:00Z",
  max_confidence: "high",
  metadata: {},
  finding_count: 1,
  is_vulnerable: true,
  is_exploited: false,
  service_count: 1,
  services: [],
  findings: [
    {
      ...findingDetail,
    },
  ],
};

function renderWithQuery(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
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
        {ui}
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
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

afterEach(() => {
  cleanup();
  mocked.apiFetch.mockReset();
});

describe("engagement-evidence-drawer", () => {
  it("opens from finding and asset contexts", async () => {
    mocked.apiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        status: "ready",
        evidence_archive_id: "ev-1",
        storage_mode: "archived_file",
        content: "line-one",
        mode_used: "head",
        truncated: false,
        source: "archived_file",
      }),
    } as Response);

    function Harness() {
      const [selected, setSelected] = useState<string | null>(null);
      const [open, setOpen] = useState(false);
      const openPreview = (evidenceId: string) => {
        setSelected(evidenceId);
        setOpen(true);
      };
      return (
        <div>
          <EngagementFindingDetailPanel finding={findingDetail} onPreviewEvidence={openPreview} />
          <EngagementAssetDetailPanel asset={assetDetail} onPreviewEvidence={openPreview} />
          <EngagementEvidenceDrawer
            engagementId="1"
            evidence={evidenceRows}
            filters={{}}
            onFiltersChange={() => {}}
            selectedEvidenceId={selected}
            onSelectEvidence={setSelected}
            isOpen={open}
            onOpenChange={setOpen}
            showCatalog={false}
          />
        </div>
      );
    }

    const { container } = renderWithQuery(<Harness />);
    const previewButtons = container.querySelectorAll("button");
    fireEvent.click(previewButtons[0]);
    await waitFor(() => {
      expect(screen.getByText("Evidence Preview")).toBeTruthy();
    });
    fireEvent.click(previewButtons[1]);
    await waitFor(() => {
      expect(screen.getByText("Evidence Preview")).toBeTruthy();
    });
  });

  it("supports bounded preview modes and renders text output", async () => {
    mocked.apiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        status: "ready",
        evidence_archive_id: "ev-1",
        storage_mode: "archived_file",
        content: "terminal-body",
        mode_used: "head",
        truncated: true,
        source: "archived_file",
      }),
    } as Response);

    function Harness() {
      const [selected, setSelected] = useState<string | null>(null);
      const [open, setOpen] = useState(false);
      return (
        <EngagementEvidenceDrawer
          engagementId="1"
          evidence={evidenceRows}
          filters={{}}
          onFiltersChange={() => {}}
          selectedEvidenceId={selected}
          onSelectEvidence={setSelected}
          isOpen={open}
          onOpenChange={setOpen}
        />
      );
    }

    renderWithQuery(<Harness />);

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => {
      expect(screen.getByTestId("terminal-output")).toBeTruthy();
    });
    expect(screen.getByText("terminal-body")).toBeTruthy();
    expect(screen.getByText("truncated")).toBeTruthy();

    selectRadixOption("Evidence Read Mode", "Match");
    fireEvent.change(screen.getByLabelText("Evidence Match Query"), { target: { value: "openssl" } });

    await waitFor(() => {
      expect(mocked.apiFetch).toHaveBeenCalled();
    });
    const lastBody = String(mocked.apiFetch.mock.calls.at(-1)?.[1]?.body ?? "");
    expect(lastBody).toContain("\"mode\":\"match\"");
    expect(lastBody).toContain("\"query\":\"openssl\"");
  });

  it("shows unavailable state when content cannot be read", async () => {
    mocked.apiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        status: "not_available",
        evidence_archive_id: "ev-1",
        storage_mode: "metadata_only",
        content: null,
        mode_used: "head",
        truncated: false,
        source: "none",
      }),
    } as Response);

    function Harness() {
      const [selected, setSelected] = useState<string | null>(null);
      const [open, setOpen] = useState(false);
      return (
        <EngagementEvidenceDrawer
          engagementId="1"
          evidence={evidenceRows}
          filters={{}}
          onFiltersChange={() => {}}
          selectedEvidenceId={selected}
          onSelectEvidence={setSelected}
          isOpen={open}
          onOpenChange={setOpen}
        />
      );
    }

    renderWithQuery(<Harness />);

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => {
      expect(
        screen.getByText("Evidence content is metadata-only or unavailable."),
      ).toBeTruthy();
    });
  });

  it("renders explicit empty and loading states for the evidence catalog", () => {
    renderWithQuery(
      <EngagementEvidenceDrawer
        engagementId="1"
        evidence={[]}
        filters={{}}
        onFiltersChange={() => {}}
        selectedEvidenceId={null}
        onSelectEvidence={() => {}}
        isOpen={false}
        onOpenChange={() => {}}
        emptyMessage="No durable evidence has been archived for this engagement yet."
      />,
    );
    expect(screen.getByText("No durable evidence has been archived for this engagement yet.")).toBeTruthy();

    cleanup();

    renderWithQuery(
      <EngagementEvidenceDrawer
        engagementId="1"
        evidence={[]}
        filters={{}}
        onFiltersChange={() => {}}
        selectedEvidenceId={null}
        onSelectEvidence={() => {}}
        isOpen={false}
        onOpenChange={() => {}}
        isLoading
      />,
    );
    expect(screen.getByLabelText("evidence-loading-skeleton")).toBeTruthy();
  });
});
