// @vitest-environment jsdom
/* Territory host-scope tests for the user-scoped knowledge workspace page. */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import KnowledgeWorkspacePage from "@/pages/knowledge-workspace-page";

const mockedKnowledgeHooks = vi.hoisted(() => ({
  useKnowledgeSummary: vi.fn(),
  useKnowledgeFindings: vi.fn(),
  useKnowledgeFinding: vi.fn(),
  useKnowledgeAssets: vi.fn(),
  useKnowledgeAsset: vi.fn(),
  useKnowledgeEvidence: vi.fn(),
  useKnowledgeRefresh: vi.fn(),
}));

const mockedEngagementHooks = vi.hoisted(() => ({
  useEngagements: vi.fn(),
  useEngagementGraph: vi.fn(),
  useEngagementFindings: vi.fn(),
  useEngagementEvidence: vi.fn(),
  useEngagementKnowledgeRefresh: vi.fn(),
}));

vi.mock("@/hooks/use-knowledge", () => ({
  useKnowledgeSummary: mockedKnowledgeHooks.useKnowledgeSummary,
  useKnowledgeFindings: mockedKnowledgeHooks.useKnowledgeFindings,
  useKnowledgeFinding: mockedKnowledgeHooks.useKnowledgeFinding,
  useKnowledgeAssets: mockedKnowledgeHooks.useKnowledgeAssets,
  useKnowledgeAsset: mockedKnowledgeHooks.useKnowledgeAsset,
  useKnowledgeEvidence: mockedKnowledgeHooks.useKnowledgeEvidence,
  useKnowledgeRefresh: mockedKnowledgeHooks.useKnowledgeRefresh,
}));

vi.mock("@/hooks/use-engagement-knowledge", () => ({
  useEngagements: mockedEngagementHooks.useEngagements,
  useEngagementGraph: mockedEngagementHooks.useEngagementGraph,
  useEngagementFindings: mockedEngagementHooks.useEngagementFindings,
  useEngagementEvidence: mockedEngagementHooks.useEngagementEvidence,
  useEngagementKnowledgeRefresh: mockedEngagementHooks.useEngagementKnowledgeRefresh,
}));

vi.mock("@/components/layout/navbar", () => ({ Navbar: () => <div data-testid="navbar" /> }));
vi.mock("@/components/layout/sidebar", () => ({ Sidebar: () => <div data-testid="sidebar" /> }));

vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock("@/components/engagements/engagement-summary-cards", () => ({
  EngagementSummaryCards: () => <div data-testid="summary-cards" />,
}));

vi.mock("@/components/engagements/engagement-findings-view", () => ({
  EngagementFindingsView: ({
    findings,
    selectedFindingId,
    onSelectFinding,
    onFiltersChange,
    findingDetail,
    errorMessage,
    detailErrorMessage,
  }: {
    findings: Array<{ id: string; title?: string | null }>;
    selectedFindingId: string | null;
    onSelectFinding: (findingId: string) => void;
    onFiltersChange: (filters: Record<string, unknown>) => void;
    findingDetail?: { title?: string | null };
    errorMessage?: string | null;
    detailErrorMessage?: string | null;
  }) => (
    <div data-testid="findings-view">
      <p data-testid="selected-finding-id">{selectedFindingId || ""}</p>
      <button type="button" onClick={() => findings[0] && onSelectFinding(findings[0].id)}>
        Select first finding
      </button>
      <button type="button" onClick={() => onFiltersChange({ query: "changed" })}>
        Change finding filters
      </button>
      {findingDetail?.title && <p>{findingDetail.title}</p>}
      {errorMessage && <p>{errorMessage}</p>}
      {detailErrorMessage && <p>{detailErrorMessage}</p>}
    </div>
  ),
}));

vi.mock("@/components/engagements/engagement-assets-view", () => ({
  EngagementAssetsView: () => <div data-testid="assets-view" />,
}));

vi.mock("@/components/engagements/engagement-evidence-view", () => ({
  EngagementEvidenceView: () => <div data-testid="evidence-view" />,
}));

vi.mock("@/components/engagements/engagement-evidence-drawer", () => ({
  EngagementEvidenceDrawer: () => <div data-testid="evidence-drawer" />,
}));

vi.mock("@/components/engagements/engagement-map-view", () => ({
  EngagementMapView: () => <div data-testid="map-view">map-view</div>,
}));

vi.mock("@/components/engagements/engagement-workspace-shell", () => ({
  EngagementWorkspaceShell: ({
    tabs,
    activeTab,
    onTabChange,
    children,
  }: {
    tabs: Array<{ id: string; label: string }>;
    activeTab: string;
    onTabChange: (tabId: string) => void;
    children: React.ReactNode;
  }) => (
    <div>
      <div>
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            aria-current={activeTab === tab.id ? "page" : undefined}
            onClick={() => onTabChange(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {children}
    </div>
  ),
}));

describe("knowledge-workspace Territory host scope", () => {
  beforeEach(() => {
    window.history.pushState({}, "", "/knowledge");
    mockedKnowledgeHooks.useKnowledgeSummary.mockReturnValue({
      data: {},
      isLoading: false,
      isError: false,
    });
    mockedKnowledgeHooks.useKnowledgeFindings.mockReturnValue({
      data: { items: [] },
      isLoading: false,
      isError: false,
    });
    mockedKnowledgeHooks.useKnowledgeFinding.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
    });
    mockedKnowledgeHooks.useKnowledgeAssets.mockReturnValue({
      data: { items: [] },
      isLoading: false,
      isError: false,
    });
    mockedKnowledgeHooks.useKnowledgeAsset.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
    });
    mockedKnowledgeHooks.useKnowledgeEvidence.mockReturnValue({
      data: { items: [] },
      isLoading: false,
      isError: false,
    });
    mockedKnowledgeHooks.useKnowledgeRefresh.mockReturnValue({
      refresh: vi.fn(async () => undefined),
      isRefreshing: false,
    });

    mockedEngagementHooks.useEngagementKnowledgeRefresh.mockReturnValue({
      refresh: vi.fn(async () => undefined),
      isRefreshing: false,
    });
    mockedEngagementHooks.useEngagements.mockReturnValue({
      data: {
        items: [
          {
            id: 7,
            user_id: 1,
            name: "Engagement Seven",
            description: null,
            status: "active",
            metadata: {},
            created_at: null,
            updated_at: null,
          },
        ],
      },
      isLoading: false,
      isError: false,
    });
    mockedEngagementHooks.useEngagementGraph.mockReturnValue({
      data: { engagement_id: 7, nodes: [], edges: [] },
      isLoading: false,
      isError: false,
    });
    mockedEngagementHooks.useEngagementFindings.mockReturnValue({
      data: { items: [] },
      isLoading: false,
      isError: false,
    });
    mockedEngagementHooks.useEngagementEvidence.mockReturnValue({
      data: { items: [] },
      isLoading: false,
      isError: false,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows scoped-empty CTA for Territory until engagement is selected", async () => {
    render(<KnowledgeWorkspacePage />);
    fireEvent.click(screen.getByRole("button", { name: "Territory" }));

    await waitFor(() => {
      expect(screen.getByText("Territory scope requires an engagement.")).toBeTruthy();
    });
  });

  it("opens Assets from the tab query parameter", () => {
    window.history.pushState({}, "", "/knowledge?tab=assets");

    render(<KnowledgeWorkspacePage />);

    expect(screen.getByTestId("assets-view")).toBeTruthy();
  });

  it("keeps summary cards in normal document flow so tab content is not obscured", () => {
    render(<KnowledgeWorkspacePage />);

    const summarySection = screen.getByTestId("knowledge-summary-section");

    expect(summarySection.className).not.toContain("sticky");
    expect(summarySection.className).not.toContain("z-20");
  });

  it("wires finding row selection into the detail query and panel", async () => {
    mockedKnowledgeHooks.useKnowledgeFindings.mockReturnValue({
      data: { items: [{ id: "finding/one", title: "Credential finding" }] },
      isLoading: false,
      isError: false,
    });
    mockedKnowledgeHooks.useKnowledgeFinding.mockImplementation((findingId: string | null) => ({
      data: findingId === "finding/one" ? { id: findingId, title: "Loaded finding detail" } : undefined,
      isLoading: false,
      isError: false,
    }));

    render(<KnowledgeWorkspacePage />);
    fireEvent.click(screen.getByRole("button", { name: "Findings" }));
    fireEvent.click(screen.getByRole("button", { name: "Select first finding" }));

    await waitFor(() => {
      expect(screen.getByTestId("selected-finding-id").textContent).toBe("finding/one");
      expect(screen.getByText("Loaded finding detail")).toBeTruthy();
    });
    expect(mockedKnowledgeHooks.useKnowledgeFinding).toHaveBeenLastCalledWith("finding/one");
  });

  it("clears stale selected finding when refreshed findings no longer contain it", async () => {
    let findingItems = [{ id: "finding-one", title: "First finding" }];
    mockedKnowledgeHooks.useKnowledgeFindings.mockImplementation(() => ({
      data: { items: findingItems },
      isLoading: false,
      isError: false,
    }));
    mockedKnowledgeHooks.useKnowledgeFinding.mockImplementation((findingId: string | null) => ({
      data: findingId ? { id: findingId, title: "Loaded finding detail" } : undefined,
      isLoading: false,
      isError: false,
    }));

    render(<KnowledgeWorkspacePage />);
    fireEvent.click(screen.getByRole("button", { name: "Findings" }));
    fireEvent.click(screen.getByRole("button", { name: "Select first finding" }));

    await waitFor(() => {
      expect(screen.getByTestId("selected-finding-id").textContent).toBe("finding-one");
    });

    findingItems = [{ id: "finding-two", title: "Second finding" }];
    fireEvent.click(screen.getByRole("button", { name: "Change finding filters" }));

    await waitFor(() => {
      expect(screen.getByTestId("selected-finding-id").textContent).toBe("");
    });
  });

  it("preserves selected finding when the findings list refresh fails", async () => {
    let isListError = false;
    mockedKnowledgeHooks.useKnowledgeFindings.mockImplementation(() => ({
      data: isListError ? undefined : { items: [{ id: "finding-one", title: "First finding" }] },
      isLoading: false,
      isError: isListError,
    }));
    mockedKnowledgeHooks.useKnowledgeFinding.mockImplementation((findingId: string | null) => ({
      data: findingId ? { id: findingId, title: "Loaded finding detail" } : undefined,
      isLoading: false,
      isError: false,
    }));

    render(<KnowledgeWorkspacePage />);
    fireEvent.click(screen.getByRole("button", { name: "Findings" }));
    fireEvent.click(screen.getByRole("button", { name: "Select first finding" }));

    await waitFor(() => {
      expect(screen.getByTestId("selected-finding-id").textContent).toBe("finding-one");
    });

    isListError = true;
    fireEvent.click(screen.getByRole("button", { name: "Change finding filters" }));

    await waitFor(() => {
      expect(screen.getByTestId("selected-finding-id").textContent).toBe("finding-one");
      expect(screen.getByText("Failed to load findings.")).toBeTruthy();
    });
  });

  it("passes selected engagement id to Territory graph host wiring", async () => {
    render(<KnowledgeWorkspacePage />);
    fireEvent.click(screen.getByRole("button", { name: "Territory" }));

    const selector = await screen.findByLabelText("Engagement");
    fireEvent.change(selector, { target: { value: "7" } });

    await waitFor(() => {
      expect(screen.getByTestId("map-view").textContent).toContain("map-view");
      expect(mockedEngagementHooks.useEngagementGraph).toHaveBeenLastCalledWith(7);
    });
  });
});
