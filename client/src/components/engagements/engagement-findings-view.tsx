/* Findings workspace view for engagement-scoped triage and detail inspection. */

import { Card, CardContent } from "@/components/ui/card";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { engagementCardClass } from "@/components/engagements/engagement-ui";
import { EngagementFindingsTable } from "@/components/engagements/engagement-findings-table";
import { EngagementFindingDetailPanel } from "@/components/engagements/engagement-finding-detail-panel";
import type {
  FindingDetail,
  FindingListItem,
  FindingsFilters,
} from "@/types/engagement-knowledge";

interface EngagementFindingsViewProps {
  findings: FindingListItem[];
  filters: FindingsFilters;
  onFiltersChange: (filters: FindingsFilters) => void;
  selectedFindingId: string | null;
  onSelectFinding: (findingId: string) => void;
  findingDetail?: FindingDetail;
  isLoading?: boolean;
  detailLoading?: boolean;
  errorMessage?: string | null;
  detailErrorMessage?: string | null;
  emptyMessage?: string;
  onPreviewEvidence: (evidenceId: string) => void;
  onOpenAsset: (assetId: string) => void;
}

export function EngagementFindingsView({
  findings,
  filters,
  onFiltersChange,
  selectedFindingId,
  onSelectFinding,
  findingDetail,
  isLoading = false,
  detailLoading = false,
  errorMessage = null,
  detailErrorMessage = null,
  emptyMessage,
  onPreviewEvidence,
  onOpenAsset,
}: EngagementFindingsViewProps) {
  if (errorMessage) {
    return (
      <Card className="rounded-xl border-red-900/80 bg-red-950/20">
        <CardContent className="p-6 text-red-300">{errorMessage}</CardContent>
      </Card>
    );
  }

  return (
    <ResizablePanelGroup direction="horizontal" className={`min-h-[540px] ${engagementCardClass} p-3`}>
      <ResizablePanel defaultSize={66} minSize={50} className="min-w-0 overflow-hidden">
        <EngagementFindingsTable
          findings={findings}
          filters={filters}
          onFiltersChange={onFiltersChange}
          selectedFindingId={selectedFindingId}
          onSelectFinding={onSelectFinding}
          isLoading={isLoading}
          emptyMessage={emptyMessage}
        />
      </ResizablePanel>
      <ResizableHandle className="mx-2 w-1 rounded-full bg-slate-800/70 transition-colors hover:bg-emerald-700/60" />
      <ResizablePanel defaultSize={34} minSize={25} className="min-w-0">
        <div className="h-full">
          <EngagementFindingDetailPanel
            finding={findingDetail}
            isLoading={detailLoading}
            errorMessage={detailErrorMessage}
            onPreviewEvidence={onPreviewEvidence}
            onOpenAsset={onOpenAsset}
          />
        </div>
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}
