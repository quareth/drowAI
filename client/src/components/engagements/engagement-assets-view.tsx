/* Assets workspace view for engagement-scoped inventory and asset detail drill-down. */

import { Card, CardContent } from "@/components/ui/card";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { engagementCardClass } from "@/components/engagements/engagement-ui";
import { EngagementAssetDetailPanel } from "@/components/engagements/engagement-asset-detail-panel";
import { EngagementAssetInventory } from "@/components/engagements/engagement-asset-inventory";
import type {
  AssetDetail,
  AssetListItem,
  AssetsFilters,
} from "@/types/engagement-knowledge";

interface EngagementAssetsViewProps {
  assets: AssetListItem[];
  filters: AssetsFilters;
  onFiltersChange: (filters: AssetsFilters) => void;
  selectedAssetId: string | null;
  onSelectAsset: (assetId: string) => void;
  assetDetail?: AssetDetail;
  isLoading?: boolean;
  detailLoading?: boolean;
  errorMessage?: string | null;
  detailErrorMessage?: string | null;
  emptyMessage?: string;
  onPreviewEvidence: (evidenceId: string) => void;
}

export function EngagementAssetsView({
  assets,
  filters,
  onFiltersChange,
  selectedAssetId,
  onSelectAsset,
  assetDetail,
  isLoading = false,
  detailLoading = false,
  errorMessage = null,
  detailErrorMessage = null,
  emptyMessage,
  onPreviewEvidence,
}: EngagementAssetsViewProps) {
  if (errorMessage) {
    return (
      <Card className="rounded-xl border-red-900/80 bg-red-950/20">
        <CardContent className="p-6 text-red-300">{errorMessage}</CardContent>
      </Card>
    );
  }

  return (
    <ResizablePanelGroup direction="horizontal" className={`min-h-[540px] ${engagementCardClass} p-3`}>
      <ResizablePanel defaultSize={66} minSize={50}>
        <EngagementAssetInventory
          assets={assets}
          filters={filters}
          onFiltersChange={onFiltersChange}
          selectedAssetId={selectedAssetId}
          onSelectAsset={onSelectAsset}
          isLoading={isLoading}
          emptyMessage={emptyMessage}
        />
      </ResizablePanel>
      <ResizableHandle className="mx-2 w-1 rounded-full bg-slate-800/70 transition-colors hover:bg-emerald-700/60" />
      <ResizablePanel defaultSize={34} minSize={25}>
        <div className="h-full">
          <EngagementAssetDetailPanel
            asset={assetDetail}
            isLoading={detailLoading}
            errorMessage={detailErrorMessage}
            onPreviewEvidence={onPreviewEvidence}
          />
        </div>
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}
