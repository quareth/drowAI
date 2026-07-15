/* Asset inventory table with risk rollups and local selection for engagement-knowledge workflow. */

import type { ChangeEvent } from "react";

import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { AssetListItem, AssetsFilters } from "@/types/engagement-knowledge";
import { cn } from "@/lib/utils";
import {
  engagementFilterBarClass,
  engagementInputClass,
  engagementRowClass,
  engagementRowSelectedClass,
  engagementSelectTriggerClass,
  engagementTableHeadClass,
} from "@/components/engagements/engagement-ui";
import { useUserTimezone } from "@/hooks/use-user-timezone";
import { formatDateTime } from "@/utils/datetime";

interface EngagementAssetInventoryProps {
  assets: AssetListItem[];
  filters: AssetsFilters;
  onFiltersChange: (filters: AssetsFilters) => void;
  selectedAssetId?: string | null;
  onSelectAsset: (assetId: string) => void;
  isLoading?: boolean;
  emptyMessage?: string;
}

function parseBooleanFilter(value: string): boolean | undefined {
  if (value === "true") {
    return true;
  }
  if (value === "false") {
    return false;
  }
  return undefined;
}

export function EngagementAssetInventory({
  assets,
  filters,
  onFiltersChange,
  selectedAssetId,
  onSelectAsset,
  isLoading = false,
  emptyMessage = "No assets matched the current filters.",
}: EngagementAssetInventoryProps) {
  const timezone = useUserTimezone();

  const handleInputFilter =
    (key: keyof AssetsFilters) => (event: ChangeEvent<HTMLInputElement>) => {
      onFiltersChange({
        ...filters,
        [key]: event.target.value || undefined,
      });
    };

  const ALL = "__all__";

  const handleSelectFilter =
    (key: keyof AssetsFilters) => (value: string) => {
      const raw = value === ALL ? "" : value;
      const normalizedValue =
        key === "vulnerable" || key === "exploited"
          ? parseBooleanFilter(raw)
          : raw || undefined;
      onFiltersChange({
        ...filters,
        [key]: normalizedValue,
      });
    };

  return (
    <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 shadow-[0_12px_30px_-20px_rgba(15,23,42,0.85)] backdrop-blur-sm">
      <div className={`${engagementFilterBarClass} grid-cols-1 md:grid-cols-4`}>
        <Select value={filters.type || ALL} onValueChange={handleSelectFilter("type")}>
          <SelectTrigger aria-label="Asset Type Filter" className={engagementSelectTriggerClass}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All types</SelectItem>
            <SelectItem value="host.ip">Host IP</SelectItem>
            <SelectItem value="host.dns">Host DNS</SelectItem>
            <SelectItem value="service.socket">Service</SelectItem>
          </SelectContent>
        </Select>
        <Select value={filters.vulnerable === undefined ? ALL : String(filters.vulnerable)} onValueChange={handleSelectFilter("vulnerable")}>
          <SelectTrigger aria-label="Vulnerable Filter" className={engagementSelectTriggerClass}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Any vulnerability state</SelectItem>
            <SelectItem value="true">Vulnerable</SelectItem>
            <SelectItem value="false">Not vulnerable</SelectItem>
          </SelectContent>
        </Select>
        <Select value={filters.exploited === undefined ? ALL : String(filters.exploited)} onValueChange={handleSelectFilter("exploited")}>
          <SelectTrigger aria-label="Asset Exploited Filter" className={engagementSelectTriggerClass}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Any exploit state</SelectItem>
            <SelectItem value="true">Exploited</SelectItem>
            <SelectItem value="false">Not exploited</SelectItem>
          </SelectContent>
        </Select>
        <Input
          aria-label="Asset Search"
          placeholder="Search assets"
          value={filters.query || ""}
          onChange={handleInputFilter("query")}
          className={engagementInputClass}
        />
      </div>

      {isLoading ? (
        <div className="p-4 space-y-2" aria-label="assets-loading-skeleton">
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={`asset-skeleton-${index}`}
              className="h-9 animate-pulse rounded-md border border-slate-800/70 bg-slate-800/70"
            />
          ))}
        </div>
      ) : assets.length === 0 ? (
        <div className="p-6 text-sm text-slate-300">{emptyMessage}</div>
      ) : (
        <Table className="text-xs">
          <TableHeader className={engagementTableHeadClass}>
            <TableRow className="border-slate-800 hover:bg-transparent">
              <TableHead className="text-slate-400">Asset</TableHead>
              <TableHead className="text-slate-400">Type</TableHead>
              <TableHead className="text-slate-400">Risk Status</TableHead>
              <TableHead className="text-slate-400">Finding Count</TableHead>
              <TableHead className="text-slate-400">Exploited</TableHead>
              <TableHead className="text-slate-400">Service Count</TableHead>
              <TableHead className="text-slate-400">Last Seen</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {assets.map((asset, index) => (
              <TableRow
                key={asset.id}
                data-state={selectedAssetId === asset.id ? "selected" : undefined}
                className={cn(
                  engagementRowClass,
                  index % 2 === 1 && "bg-slate-900/35",
                  selectedAssetId === asset.id && engagementRowSelectedClass,
                )}
                onClick={() => onSelectAsset(asset.id)}
              >
                <TableCell className="text-slate-100">
                  {asset.display_name || asset.hostname || asset.ip_address || asset.asset_key || asset.id}
                </TableCell>
                <TableCell className="text-slate-300">{asset.asset_type || "unknown"}</TableCell>
                <TableCell>
                  {asset.is_vulnerable ? (
                    <EngagementIndicatorBadge>
                      Vulnerable
                    </EngagementIndicatorBadge>
                  ) : (
                    <EngagementIndicatorBadge>
                      Observed
                    </EngagementIndicatorBadge>
                  )}
                </TableCell>
                <TableCell>
                  <EngagementIndicatorBadge>
                    {asset.finding_count}
                  </EngagementIndicatorBadge>
                </TableCell>
                <TableCell>
                  <EngagementIndicatorBadge>
                    {asset.is_exploited ? "Yes" : "No"}
                  </EngagementIndicatorBadge>
                </TableCell>
                <TableCell>
                  <EngagementIndicatorBadge>
                    {asset.service_count}
                  </EngagementIndicatorBadge>
                </TableCell>
                <TableCell className="text-slate-300">{formatDateTime(asset.last_seen_at, timezone)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
