/* Reusable engagement evidence catalog table for inline evidence browsing. */

import { Fragment, useState } from "react";

import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type {
  EvidenceFilters,
  EvidenceListItem,
} from "@/types/knowledge";
import {
  engagementFilterBarClass,
  engagementInlineButtonClass,
  engagementInputClass,
  engagementTableHeadClass,
} from "@/components/engagements/engagement-ui";
import { useUserTimezone } from "@/hooks/use-user-timezone";
import { formatDateTime } from "@/utils/datetime";

interface EngagementEvidenceCatalogProps {
  evidence: EvidenceListItem[];
  filters: EvidenceFilters;
  onFiltersChange: (filters: EvidenceFilters) => void;
  onPreviewEvidence: (evidenceId: string) => void;
  isLoading?: boolean;
  isError?: boolean;
  errorMessage?: string | null;
  emptyMessage?: string;
}

interface EvidenceGroupMember {
  id: string;
  evidence_type: string | null;
  source_tool: string | null;
  created_at: string | null;
  storage_mode: string | null;
}

interface EvidenceExecutionGroup {
  memberCount: number;
  members: EvidenceGroupMember[];
}

function asStringOrNull(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
}

function extractExecutionGroup(item: EvidenceListItem): EvidenceExecutionGroup {
  const metadata = item.metadata as Record<string, unknown> | null;
  const rawGroup =
    metadata && typeof metadata === "object"
      ? (metadata.execution_group as Record<string, unknown> | undefined)
      : undefined;
  const rawMembers = rawGroup && Array.isArray(rawGroup.members) ? rawGroup.members : [];

  const members: EvidenceGroupMember[] = rawMembers
    .map((entry) => {
      if (!entry || typeof entry !== "object") {
        return null;
      }
      const payload = entry as Record<string, unknown>;
      const id = asStringOrNull(payload.id);
      if (!id) {
        return null;
      }
      return {
        id,
        evidence_type: asStringOrNull(payload.evidence_type),
        source_tool: asStringOrNull(payload.source_tool),
        created_at: asStringOrNull(payload.created_at),
        storage_mode: asStringOrNull(payload.storage_mode),
      };
    })
    .filter((entry): entry is EvidenceGroupMember => Boolean(entry));

  if (members.length === 0) {
    return {
      memberCount: 1,
      members: [
        {
          id: item.id,
          evidence_type: item.evidence_type,
          source_tool: item.source_tool,
          created_at: item.created_at,
          storage_mode: item.storage_mode,
        },
      ],
    };
  }

  return {
    memberCount: members.length,
    members,
  };
}

export function EngagementEvidenceCatalog({
  evidence,
  filters,
  onFiltersChange,
  onPreviewEvidence,
  isLoading = false,
  isError = false,
  errorMessage = null,
  emptyMessage = "No evidence matched the current filters.",
}: EngagementEvidenceCatalogProps) {
  const timezone = useUserTimezone();
  const [expandedExecutionIds, setExpandedExecutionIds] = useState<Set<string>>(new Set());

  const toggleExecutionGroup = (executionId: string) => {
    setExpandedExecutionIds((previous) => {
      const next = new Set(previous);
      if (next.has(executionId)) {
        next.delete(executionId);
      } else {
        next.add(executionId);
      }
      return next;
    });
  };

  return (
    <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 shadow-[0_12px_30px_-20px_rgba(15,23,42,0.85)] backdrop-blur-sm">
      <div className={`${engagementFilterBarClass} grid-cols-1 md:grid-cols-3`}>
        <Input
          aria-label="Evidence Source Filter"
          placeholder="Source tool"
          value={filters.source_tool || ""}
          onChange={(event) =>
            onFiltersChange({
              ...filters,
              source_tool: event.target.value || undefined,
            })
          }
          className={engagementInputClass}
        />
        <Input
          aria-label="Evidence Type Filter"
          placeholder="Evidence type"
          value={filters.type || ""}
          onChange={(event) =>
            onFiltersChange({
              ...filters,
              type: event.target.value || undefined,
            })
          }
          className={engagementInputClass}
        />
        <Input
          aria-label="Evidence Search"
          placeholder="Search evidence"
          value={filters.query || ""}
          onChange={(event) =>
            onFiltersChange({
              ...filters,
              query: event.target.value || undefined,
            })
          }
          className={engagementInputClass}
        />
      </div>

      {isError ? (
        <div className="p-6 text-sm text-red-300">
          {errorMessage || "Failed to load evidence for this engagement."}
        </div>
      ) : isLoading ? (
        <div className="p-4 space-y-2" aria-label="evidence-loading-skeleton">
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={`evidence-skeleton-${index}`}
              className="h-9 animate-pulse rounded-md border border-slate-800/70 bg-slate-800/70"
            />
          ))}
        </div>
      ) : evidence.length === 0 ? (
        <div className="p-6 text-sm text-slate-300">{emptyMessage}</div>
      ) : (
        <Table className="text-xs">
          <TableHeader className={engagementTableHeadClass}>
            <TableRow className="border-slate-800 hover:bg-transparent">
              <TableHead className="text-slate-400">Evidence</TableHead>
              <TableHead className="text-slate-400">Type</TableHead>
              <TableHead className="text-slate-400">Source Tool</TableHead>
              <TableHead className="text-slate-400">Lineage</TableHead>
              <TableHead className="text-slate-400">Observed</TableHead>
              <TableHead className="text-slate-400">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {evidence.map((item, index) => {
              const executionGroup = extractExecutionGroup(item);
              const executionId = item.source_execution_id;
              const isExpanded = expandedExecutionIds.has(executionId);
              const hasMultipleMembers = executionGroup.members.length > 1;
              return (
                <Fragment key={item.id}>
                  <TableRow
                    className={`border-slate-800/90 transition-colors duration-150 hover:bg-slate-800/45 ${index % 2 === 1 ? "bg-slate-900/35" : "bg-slate-900/20"} ${hasMultipleMembers ? "cursor-pointer" : ""}`}
                    onClick={() => {
                      if (hasMultipleMembers) {
                        toggleExecutionGroup(executionId);
                      }
                    }}
                  >
                    <TableCell className="text-slate-200">{item.id}</TableCell>
                    <TableCell className="text-slate-300">
                      <div className="flex items-center gap-1.5">
                        <span>{item.evidence_type || item.storage_mode || "-"}</span>
                        {hasMultipleMembers && (
                          <EngagementIndicatorBadge>
                            {executionGroup.memberCount} entries
                          </EngagementIndicatorBadge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-slate-300">{item.source_tool || "-"}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {item.task_id && (
                          <EngagementIndicatorBadge>
                            task:{item.task_id}
                          </EngagementIndicatorBadge>
                        )}
                        <EngagementIndicatorBadge>
                          exec:{item.source_execution_id.slice(0, 8)}
                        </EngagementIndicatorBadge>
                      </div>
                    </TableCell>
                    <TableCell className="text-slate-300">{formatDateTime(item.created_at, timezone)}</TableCell>
                    <TableCell>
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          onPreviewEvidence(item.id);
                        }}
                        className={engagementInlineButtonClass}
                      >
                        Preview
                      </button>
                    </TableCell>
                  </TableRow>
                  {hasMultipleMembers && isExpanded && (
                    <TableRow className="border-slate-800/70 bg-slate-950/55">
                      <TableCell colSpan={6}>
                        <div className="space-y-2">
                          {executionGroup.members.map((member) => (
                            <div
                              key={`${item.id}-member-${member.id}`}
                              className="flex items-center justify-between rounded-md border border-slate-800/80 bg-slate-900/55 px-3 py-2"
                            >
                              <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-300">
                                <span className="font-mono text-slate-200">{member.id}</span>
                                <EngagementIndicatorBadge>
                                  {member.evidence_type || member.storage_mode || "-"}
                                </EngagementIndicatorBadge>
                                <span>{formatDateTime(member.created_at, timezone)}</span>
                              </div>
                              <button
                                type="button"
                                onClick={(event) => {
                                  event.stopPropagation();
                                  onPreviewEvidence(member.id);
                                }}
                                className={engagementInlineButtonClass}
                              >
                                Preview
                              </button>
                            </div>
                          ))}
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              );
            })}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
