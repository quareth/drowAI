/* Findings-first table with filters and row selection for engagement-knowledge triage. */

import type { ChangeEvent } from "react";

import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import {
  FindingSeverityBadge,
  FindingStatusBadge,
} from "@/components/engagements/finding-badges";
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
import type { FindingListItem, FindingsFilters } from "@/types/engagement-knowledge";
import { cn } from "@/lib/utils";
import {
  engagementFilterBarClass,
  engagementInputClass,
  engagementRowClass,
  engagementRowSelectedClass,
  engagementSelectTriggerClass,
  engagementTableHeadClass,
} from "@/components/engagements/engagement-ui";
import {
  FINDING_SEVERITY_FILTER_OPTIONS,
} from "@/components/engagements/severity-presentation";
import { useUserTimezone } from "@/hooks/use-user-timezone";
import { formatDateTime } from "@/utils/datetime";

interface EngagementFindingsTableProps {
  findings: FindingListItem[];
  filters: FindingsFilters;
  onFiltersChange: (filters: FindingsFilters) => void;
  selectedFindingId?: string | null;
  onSelectFinding: (findingId: string) => void;
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

function resolveAffectedAssetCount(finding: FindingListItem): number {
  if (typeof finding.affected_asset_count === "number" && Number.isFinite(finding.affected_asset_count)) {
    return Math.max(0, Math.trunc(finding.affected_asset_count));
  }
  return finding.asset_id ? 1 : 0;
}

function resolveAffectedAssetLabel(finding: FindingListItem): string {
  const asset = finding.asset;
  return (
    asset?.display_name ||
    asset?.hostname ||
    asset?.ip_address ||
    asset?.asset_key ||
    finding.asset_id ||
    "-"
  );
}

const compactTableCellClass = "px-3 py-4";
const supportingColumnClass = "hidden 2xl:table-cell";

export function EngagementFindingsTable({
  findings,
  filters,
  onFiltersChange,
  selectedFindingId,
  onSelectFinding,
  isLoading = false,
  emptyMessage = "No findings matched the current filters.",
}: EngagementFindingsTableProps) {
  const timezone = useUserTimezone();

  const handleInputFilter =
    (key: keyof FindingsFilters) => (event: ChangeEvent<HTMLInputElement>) => {
      onFiltersChange({
        ...filters,
        [key]: event.target.value || undefined,
      });
    };

  const ALL = "__all__";

  const handleSelectFilter =
    (key: keyof FindingsFilters) => (value: string) => {
      const raw = value === ALL ? "" : value;
      const nextValue =
        key === "exploited" ? parseBooleanFilter(raw) : raw || undefined;
      onFiltersChange({
        ...filters,
        [key]: nextValue,
      });
    };

  return (
    <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 shadow-[0_12px_30px_-20px_rgba(15,23,42,0.85)] backdrop-blur-sm">
      <div className={`${engagementFilterBarClass} grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6`}>
        <Select value={filters.severity || ALL} onValueChange={handleSelectFilter("severity")}>
          <SelectTrigger aria-label="Severity Filter" className={engagementSelectTriggerClass}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All severities</SelectItem>
            {FINDING_SEVERITY_FILTER_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={filters.status || ALL} onValueChange={handleSelectFilter("status")}>
          <SelectTrigger aria-label="Status Filter" className={engagementSelectTriggerClass}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All statuses</SelectItem>
            <SelectItem value="open">Open</SelectItem>
            <SelectItem value="confirmed">Confirmed</SelectItem>
            <SelectItem value="triaged">Triaged</SelectItem>
            <SelectItem value="exploited">Exploited</SelectItem>
          </SelectContent>
        </Select>
        <Select value={filters.exploited === undefined ? ALL : String(filters.exploited)} onValueChange={handleSelectFilter("exploited")}>
          <SelectTrigger aria-label="Exploited Filter" className={engagementSelectTriggerClass}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Any exploit state</SelectItem>
            <SelectItem value="true">Exploited</SelectItem>
            <SelectItem value="false">Not exploited</SelectItem>
          </SelectContent>
        </Select>
        <Input
          aria-label="Source Filter"
          placeholder="Source tool"
          value={filters.source || ""}
          onChange={handleInputFilter("source")}
          className={engagementInputClass}
        />
        <Input
          aria-label="Asset Filter"
          placeholder="Asset key/id"
          value={filters.asset || ""}
          onChange={handleInputFilter("asset")}
          className={engagementInputClass}
        />
        <Input
          aria-label="Search Findings"
          placeholder="Search findings"
          value={filters.query || ""}
          onChange={handleInputFilter("query")}
          className={engagementInputClass}
        />
      </div>

      {isLoading ? (
        <div className="p-4 space-y-2" aria-label="findings-loading-skeleton">
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={`finding-skeleton-${index}`}
              className="h-9 animate-pulse rounded-md border border-slate-800/70 bg-slate-800/70"
            />
          ))}
        </div>
      ) : findings.length === 0 ? (
        <div className="p-6 text-sm text-slate-300">{emptyMessage}</div>
      ) : (
        <Table className="min-w-[700px] table-fixed text-xs">
          <TableHeader className={engagementTableHeadClass}>
            <TableRow className="border-slate-800 hover:bg-transparent">
              <TableHead className="w-[92px] px-3 text-slate-400">Severity</TableHead>
              <TableHead className="w-[240px] px-3 text-slate-400">Title</TableHead>
              <TableHead className="w-[108px] px-3 text-slate-400">Status</TableHead>
              <TableHead className={`${supportingColumnClass} w-[180px] px-3 text-slate-400`}>
                Affected Asset
              </TableHead>
              <TableHead className="w-[92px] px-3 text-slate-400">Affected Assets</TableHead>
              <TableHead className="w-[96px] px-3 text-slate-400">Source</TableHead>
              <TableHead className="w-[72px] px-3 text-slate-400">Evidence</TableHead>
              <TableHead className={`${supportingColumnClass} w-[150px] px-3 text-slate-400`}>
                Last Observed
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {findings.map((finding, index) => {
              const sourceTool = finding.source_tool || "unknown";
              const affectedAssetCount = resolveAffectedAssetCount(finding);
              const displayTitle = finding.title || finding.finding_key || "Untitled finding";
              const affectedAsset = resolveAffectedAssetLabel(finding);
              return (
                <TableRow
                  key={finding.id}
                  data-state={selectedFindingId === finding.id ? "selected" : undefined}
                  className={cn(
                    engagementRowClass,
                    index % 2 === 1 && "bg-slate-900/35",
                    selectedFindingId === finding.id && engagementRowSelectedClass,
                  )}
                  onClick={() => onSelectFinding(finding.id)}
                >
                  <TableCell className={compactTableCellClass}>
                    <FindingSeverityBadge severity={finding.severity} />
                  </TableCell>
                  <TableCell className={`${compactTableCellClass} max-w-0 text-slate-100`}>
                    <span className="block truncate" title={displayTitle}>
                      {displayTitle}
                    </span>
                  </TableCell>
                  <TableCell className={`${compactTableCellClass} text-slate-200`}>
                    <FindingStatusBadge
                      isExploited={finding.is_exploited}
                      status={finding.status}
                    />
                  </TableCell>
                  <TableCell className={`${supportingColumnClass} ${compactTableCellClass} text-slate-300`}>
                    <span className="block truncate" title={affectedAsset}>
                      {affectedAsset}
                    </span>
                  </TableCell>
                  <TableCell className={compactTableCellClass}>
                    <EngagementIndicatorBadge>
                      {affectedAssetCount} assets
                    </EngagementIndicatorBadge>
                  </TableCell>
                  <TableCell className={compactTableCellClass}>
                    <EngagementIndicatorBadge
                      className="max-w-full truncate"
                      title={sourceTool}
                    >
                      {sourceTool}
                    </EngagementIndicatorBadge>
                  </TableCell>
                  <TableCell className={compactTableCellClass}>
                    <EngagementIndicatorBadge>
                      {finding.evidence_count}
                    </EngagementIndicatorBadge>
                  </TableCell>
                  <TableCell className={`${supportingColumnClass} ${compactTableCellClass} text-slate-300`}>
                    {formatDateTime(finding.last_seen_at, timezone)}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
