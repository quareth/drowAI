/* User-scoped knowledge workspace with summary, findings, assets, evidence, and map tabs. */

import { useEffect, useState } from "react";
import { useLocation } from "wouter";

import { EngagementAssetsView } from "@/components/engagements/engagement-assets-view";
import { EngagementEvidenceDrawer } from "@/components/engagements/engagement-evidence-drawer";
import { EngagementEvidenceView } from "@/components/engagements/engagement-evidence-view";
import { EngagementFindingsView } from "@/components/engagements/engagement-findings-view";
import { EngagementMapView } from "@/components/engagements/engagement-map-view";
import { EngagementSummaryCards } from "@/components/engagements/engagement-summary-cards";
import {
  EngagementWorkspaceShell,
} from "@/components/engagements/engagement-workspace-shell";
import { engagementCardClass, engagementShellPanelMutedClass } from "@/components/engagements/engagement-ui";
import { Navbar } from "@/components/layout/navbar";
import { Sidebar } from "@/components/layout/sidebar";
import { Card, CardContent } from "@/components/ui/card";
import {
  useKnowledgeAsset,
  useKnowledgeAssets,
  useKnowledgeEvidence,
  useKnowledgeFindings,
  useKnowledgeFinding,
  useKnowledgeRefresh,
  useKnowledgeSummary,
} from "@/hooks/use-knowledge";
import {
  useEngagementGraph,
  useEngagementKnowledgeRefresh,
  useEngagements,
} from "@/hooks/use-engagement-knowledge";
import type {
  AssetsFilters,
  EvidenceFilters,
  EvidenceListItem,
  FindingListItem,
  FindingsFilters,
} from "@/types/knowledge";
import {
  DEFAULT_KNOWLEDGE_TAB,
  KNOWLEDGE_WORKSPACE_TABS,
  type KnowledgeWorkspaceTabId,
} from "@/pages/knowledge-workspace-navigation";
import { buildKnowledgeTabPath, readAllowedQueryValue, ROUTE_QUERY_KEYS } from "@/navigation/routes";

const KNOWLEDGE_TAB_IDS = KNOWLEDGE_WORKSPACE_TABS.map((tab) => tab.id);

/** Map knowledge FindingListItem to engagement-shaped for reuse of engagement components. */
function toEngagementFinding(item: FindingListItem): FindingListItem & { engagement_id: number } {
  return { ...item, engagement_id: 0 };
}

/** Map knowledge EvidenceListItem to engagement-shaped for reuse. */
function toEngagementEvidence(
  item: EvidenceListItem,
): EvidenceListItem & { engagement_id: number } {
  return { ...item, engagement_id: 0 } as EvidenceListItem & { engagement_id: number };
}

export default function KnowledgeWorkspacePage() {
  const [location, setLocation] = useLocation();
  const locationTab = readAllowedQueryValue(
    location,
    ROUTE_QUERY_KEYS.knowledgeTab,
    KNOWLEDGE_TAB_IDS,
    DEFAULT_KNOWLEDGE_TAB,
  );
  const [activeTab, setActiveTab] = useState<KnowledgeWorkspaceTabId>(locationTab);
  const [territoryEngagementId, setTerritoryEngagementId] = useState<number | null>(null);
  const [findingsFilters, setFindingsFilters] = useState<FindingsFilters>({
    sort: "last_seen_desc",
    limit: 50,
    offset: 0,
  });
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  const [assetsFilters, setAssetsFilters] = useState<AssetsFilters>({
    sort: "last_seen_desc",
    limit: 50,
    offset: 0,
  });
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [evidenceFilters, setEvidenceFilters] = useState<EvidenceFilters>({
    sort: "observed_desc",
    limit: 50,
    offset: 0,
  });
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null);
  const [isEvidenceDrawerOpen, setIsEvidenceDrawerOpen] = useState(false);

  const summaryQuery = useKnowledgeSummary();
  const findingsQuery = useKnowledgeFindings(findingsFilters);
  const findingDetailQuery = useKnowledgeFinding(selectedFindingId);
  const assetsQuery = useKnowledgeAssets(assetsFilters);
  const assetDetailQuery = useKnowledgeAsset(selectedAssetId);
  const evidenceQuery = useKnowledgeEvidence(evidenceFilters);
  const territoryEngagementsQuery = useEngagements({ status: "active", limit: 100, offset: 0 });
  const territoryGraphQuery = useEngagementGraph(territoryEngagementId);
  const { refresh: refreshKnowledge, isRefreshing: isKnowledgeRefreshing } = useKnowledgeRefresh();
  const { refresh: refreshEngagement, isRefreshing: isEngagementRefreshing } =
    useEngagementKnowledgeRefresh(territoryEngagementId);

  useEffect(() => {
    setActiveTab(locationTab);
  }, [locationTab]);

  const handleTabChange = (tabId: string) => {
    const nextTab = tabId as KnowledgeWorkspaceTabId;
    setActiveTab(nextTab);
    setLocation(buildKnowledgeTabPath(nextTab));
  };

  const refresh = async () => {
    await refreshKnowledge();
    if (territoryEngagementId !== null) {
      await refreshEngagement();
    }
  };
  const isRefreshing = isKnowledgeRefreshing || isEngagementRefreshing;

  const findings = findingsQuery.data?.items || [];
  const assets = assetsQuery.data?.items || [];
  const evidence = evidenceQuery.data?.items || [];

  useEffect(() => {
    if (!selectedFindingId || findingsQuery.isLoading || findingsQuery.isError || !findingsQuery.data) {
      return;
    }
    if (!findings.some((finding) => finding.id === selectedFindingId)) {
      setSelectedFindingId(null);
    }
  }, [findings, findingsQuery.data, findingsQuery.isError, findingsQuery.isLoading, selectedFindingId]);

  const openEvidencePreview = (evidenceId: string) => {
    setSelectedEvidenceId(evidenceId);
    setIsEvidenceDrawerOpen(true);
  };

  const openLinkedAssetFromFinding = (assetId: string) => {
    setSelectedAssetId(assetId);
    setActiveTab("assets");
  };

  const loading = summaryQuery.isLoading;
  const hasError = summaryQuery.isError;
  const findingsErrorMessage = findingsQuery.isError ? "Failed to load findings." : null;
  const findingDetailErrorMessage = findingDetailQuery.isError
    ? "Failed to load selected finding details."
    : null;
  const assetsErrorMessage = assetsQuery.isError ? "Failed to load assets." : null;
  const assetDetailErrorMessage = assetDetailQuery.isError
    ? "Failed to load selected asset details."
    : null;
  const evidenceErrorMessage = evidenceQuery.isError ? "Failed to load evidence." : null;
  const graphErrorMessage = territoryGraphQuery.isError
    ? "Failed to load relationship graph."
    : null;

  const hasActiveFindingFilters = Boolean(
    findingsFilters.severity ||
      findingsFilters.status ||
      findingsFilters.exploited !== undefined ||
      findingsFilters.source ||
      findingsFilters.asset ||
      findingsFilters.query,
  );
  const hasActiveAssetFilters = Boolean(
    assetsFilters.type ||
      assetsFilters.vulnerable !== undefined ||
      assetsFilters.exploited !== undefined ||
      assetsFilters.query,
  );
  const hasActiveEvidenceFilters = Boolean(
    evidenceFilters.source_tool || evidenceFilters.type || evidenceFilters.query,
  );

  const findingsEmptyMessage =
    findings.length === 0 && !hasActiveFindingFilters
      ? "No durable findings have been projected yet."
      : "No findings matched the current filters.";
  const assetsEmptyMessage =
    assets.length === 0 && !hasActiveAssetFilters
      ? "No durable assets are available yet."
      : "No assets matched the current filters.";
  const evidenceEmptyMessage =
    evidence.length === 0 && !hasActiveEvidenceFilters
      ? "No durable evidence has been archived yet."
      : "No evidence matched the current filters.";

  const renderActiveTab = () => {
    if (loading) {
      return (
        <Card className={engagementCardClass}>
          <CardContent className="p-6 text-slate-300">Loading knowledge workspace...</CardContent>
        </Card>
      );
    }

    if (hasError) {
      return (
        <Card className="rounded-xl border-red-900/80 bg-red-950/20 shadow-[0_10px_25px_-18px_rgba(239,68,68,0.8)]">
          <CardContent className="p-6 text-red-300">Failed to load knowledge workspace.</CardContent>
        </Card>
      );
    }

    switch (activeTab) {
      case "summary":
        return (
          <Card className={engagementCardClass}>
            <CardContent className="p-6">
              <h2 className="text-lg font-semibold text-white mb-4">Knowledge Overview</h2>
              <p className="text-sm text-slate-300">
                Durable user-scoped security knowledge is active. Use Findings, Assets, Evidence, and
                Territory tabs to explore canonical records and provenance.
              </p>
            </CardContent>
          </Card>
        );
      case "findings":
        return (
          <EngagementFindingsView
            findings={findings.map(toEngagementFinding)}
            filters={findingsFilters as Parameters<typeof EngagementFindingsView>[0]["filters"]}
            onFiltersChange={(f) => setFindingsFilters(f)}
            selectedFindingId={selectedFindingId}
            onSelectFinding={setSelectedFindingId}
            findingDetail={
              findingDetailQuery.data
                ? (findingDetailQuery.data as Parameters<typeof EngagementFindingsView>[0]["findingDetail"])
                : undefined
            }
            isLoading={findingsQuery.isLoading}
            detailLoading={findingDetailQuery.isLoading}
            errorMessage={findingsErrorMessage}
            detailErrorMessage={findingDetailErrorMessage}
            emptyMessage={findingsEmptyMessage}
            onPreviewEvidence={openEvidencePreview}
            onOpenAsset={openLinkedAssetFromFinding}
          />
        );
      case "assets":
        return (
          <EngagementAssetsView
            assets={assets.map((a) => ({ ...a, engagement_id: 0 }))}
            filters={assetsFilters as Parameters<typeof EngagementAssetsView>[0]["filters"]}
            onFiltersChange={(f) => setAssetsFilters(f)}
            selectedAssetId={selectedAssetId}
            onSelectAsset={setSelectedAssetId}
            assetDetail={
              assetDetailQuery.data
                ? (assetDetailQuery.data as Parameters<typeof EngagementAssetsView>[0]["assetDetail"])
                : undefined
            }
            isLoading={assetsQuery.isLoading}
            detailLoading={assetDetailQuery.isLoading}
            errorMessage={assetsErrorMessage}
            detailErrorMessage={assetDetailErrorMessage}
            emptyMessage={assetsEmptyMessage}
            onPreviewEvidence={openEvidencePreview}
          />
        );
      case "evidence":
        return (
          <EngagementEvidenceView
            evidence={evidence.map(toEngagementEvidence)}
            filters={evidenceFilters as Parameters<typeof EngagementEvidenceView>[0]["filters"]}
            onFiltersChange={(f) => setEvidenceFilters(f)}
            isLoading={evidenceQuery.isLoading}
            isError={evidenceQuery.isError}
            errorMessage={evidenceErrorMessage}
            emptyMessage={evidenceEmptyMessage}
            onPreviewEvidence={openEvidencePreview}
          />
        );
      case "map":
        if (territoryEngagementId === null) {
          return (
            <Card className={engagementCardClass}>
              <CardContent className="space-y-3 p-6">
                <p className="text-sm text-slate-200">Territory scope requires an engagement.</p>
                <p className="text-xs text-slate-400">
                  Select an engagement to load engagement-scoped graph and web-surface context.
                </p>
                <label className="block text-xs text-slate-300">
                  Engagement
                  <select
                    className="mt-1 block w-full rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100"
                    value={territoryEngagementId === null ? "" : String(territoryEngagementId)}
                    onChange={(event) => {
                      const normalized = event.currentTarget.value.trim();
                      setTerritoryEngagementId(normalized ? Number(normalized) : null);
                    }}
                  >
                    <option value="">Select engagement...</option>
                    {(territoryEngagementsQuery.data?.items || []).map((engagement) => (
                      <option key={engagement.id} value={engagement.id}>
                        {engagement.name}
                      </option>
                    ))}
                  </select>
                </label>
              </CardContent>
            </Card>
          );
        }
        return (
          <div className="space-y-3">
            <label className="block text-xs text-slate-300">
              Engagement
              <select
                className="mt-1 block w-full rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100"
                value={String(territoryEngagementId)}
                onChange={(event) => {
                  const normalized = event.currentTarget.value.trim();
                  setTerritoryEngagementId(normalized ? Number(normalized) : null);
                }}
              >
                {(territoryEngagementsQuery.data?.items || []).map((engagement) => (
                  <option key={engagement.id} value={engagement.id}>
                    {engagement.name}
                  </option>
                ))}
              </select>
            </label>
            <EngagementMapView
              graph={territoryGraphQuery.data as Parameters<typeof EngagementMapView>[0]["graph"]}
              isLoading={territoryGraphQuery.isLoading}
              errorMessage={graphErrorMessage}
              onSelectNode={() => undefined}
              onSelectEdge={() => undefined}
            />
          </div>
        );
      default:
        return null;
    }
  };

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      <Navbar />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <div className="flex-1 flex flex-col overflow-hidden">
          <EngagementWorkspaceShell
            tabs={KNOWLEDGE_WORKSPACE_TABS}
            activeTab={activeTab}
            onTabChange={handleTabChange}
            onRefresh={refresh}
            isRefreshing={isRefreshing}
          >
            <>
              <div className={`h-full overflow-auto p-4 md:p-5 ${engagementShellPanelMutedClass}`}>
                <div className="space-y-4">
                  <div data-testid="knowledge-summary-section" className="-mx-1 px-1">
                    <EngagementSummaryCards
                      summary={
                        summaryQuery.data
                          ? ({ ...summaryQuery.data, engagement_id: 0 } as Parameters<
                              typeof EngagementSummaryCards
                            >[0]["summary"])
                          : undefined
                      }
                      isLoading={summaryQuery.isLoading && !summaryQuery.data}
                    />
                  </div>
                  <div className="engagement-fade-in">{renderActiveTab()}</div>
                </div>
              </div>
              <EngagementEvidenceDrawer
                engagementId={null}
                useKnowledgeApi
                evidence={evidence.map(toEngagementEvidence) as Parameters<typeof EngagementEvidenceDrawer>[0]["evidence"]}
                filters={evidenceFilters as Parameters<typeof EngagementEvidenceDrawer>[0]["filters"]}
                onFiltersChange={(f) => setEvidenceFilters(f)}
                selectedEvidenceId={selectedEvidenceId}
                onSelectEvidence={setSelectedEvidenceId}
                isOpen={isEvidenceDrawerOpen}
                onOpenChange={setIsEvidenceDrawerOpen}
                isLoading={evidenceQuery.isLoading}
                isError={evidenceQuery.isError}
                errorMessage={evidenceErrorMessage}
                showCatalog={false}
                emptyMessage={evidenceEmptyMessage}
              />
            </>
          </EngagementWorkspaceShell>
        </div>
      </div>
    </div>
  );
}
