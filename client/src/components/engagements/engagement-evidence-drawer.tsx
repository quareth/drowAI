/* Evidence catalog and bounded preview drawer for engagement-knowledge surfaces. */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { ToolCardTerminalOutput } from "@/components/chat/tool-card-terminal/ToolCardTerminalOutput";
import { EngagementEvidenceCatalog } from "@/components/engagements/engagement-evidence-catalog";
import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { apiFetch } from "@/lib/api-config";
import {
  engagementDetailSectionClass,
  engagementInputClass,
  engagementSelectTriggerClass,
} from "@/components/engagements/engagement-ui";
import type {
  EvidenceFilters,
  EvidenceListItem,
  EvidenceReadMode,
  EvidenceReadResponse,
} from "@/types/knowledge";

interface EngagementEvidenceDrawerProps {
  engagementId: string | null | undefined;
  /** When true, uses /api/knowledge/evidence for user-scoped reads (engagementId ignored). */
  useKnowledgeApi?: boolean;
  evidence: EvidenceListItem[];
  filters: EvidenceFilters;
  onFiltersChange: (filters: EvidenceFilters) => void;
  selectedEvidenceId: string | null;
  onSelectEvidence: (evidenceId: string) => void;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  isLoading?: boolean;
  isError?: boolean;
  errorMessage?: string | null;
  showCatalog?: boolean;
  emptyMessage?: string;
}

async function readEngagementEvidence(
  engagementId: string,
  evidenceId: string,
  mode: EvidenceReadMode,
  query: string,
): Promise<EvidenceReadResponse> {
  const response = await apiFetch(
    `/api/engagements/${encodeURIComponent(engagementId)}/evidence/${encodeURIComponent(evidenceId)}/read`,
    {
      method: "POST",
      body: JSON.stringify({
        mode,
        query: query.trim() || null,
        max_chars: 4000,
      }),
    },
  );
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Evidence preview failed (${response.status})`);
  }
  return response.json() as Promise<EvidenceReadResponse>;
}

async function readKnowledgeEvidence(
  evidenceId: string,
  mode: EvidenceReadMode,
): Promise<EvidenceReadResponse> {
  const response = await apiFetch(
    `/api/knowledge/evidence/${encodeURIComponent(evidenceId)}/read`,
    {
      method: "POST",
      body: JSON.stringify({
        mode: mode === "auto" ? "head" : mode,
        max_bytes: 8192,
      }),
    },
  );
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Evidence preview failed (${response.status})`);
  }
  const data = (await response.json()) as {
    status: string;
    evidence_archive_id: string;
    storage_mode: string;
    content: string | null;
    mode_used?: string;
    truncated?: boolean;
    source?: string;
  };
  return {
    status: data.status as EvidenceReadResponse["status"],
    evidence_archive_id: data.evidence_archive_id,
    storage_mode: data.storage_mode,
    content: data.content,
    mode_used: (data.mode_used || "head") as EvidenceReadMode,
    truncated: data.truncated ?? false,
    source: (data.source || "none") as EvidenceReadResponse["source"],
  };
}

export function EngagementEvidenceDrawer({
  engagementId,
  useKnowledgeApi = false,
  evidence,
  filters,
  onFiltersChange,
  selectedEvidenceId,
  onSelectEvidence,
  isOpen,
  onOpenChange,
  isLoading = false,
  isError = false,
  errorMessage = null,
  showCatalog = true,
  emptyMessage = "No evidence matched the current filters.",
}: EngagementEvidenceDrawerProps) {
  const [readMode, setReadMode] = useState<EvidenceReadMode>("head");
  const [matchQuery, setMatchQuery] = useState("");
  const selectedEvidence = useMemo(
    () => evidence.find((item) => item.id === selectedEvidenceId) || null,
    [evidence, selectedEvidenceId],
  );

  const canPreview = Boolean(isOpen && selectedEvidenceId && (useKnowledgeApi || engagementId));

  const previewQuery = useQuery<EvidenceReadResponse>({
    queryKey: [
      useKnowledgeApi ? "knowledge-evidence-preview" : "engagement-evidence-preview",
      engagementId,
      selectedEvidenceId,
      readMode,
      matchQuery,
    ],
    enabled: canPreview,
    queryFn: () =>
      useKnowledgeApi
        ? readKnowledgeEvidence(String(selectedEvidenceId), readMode)
        : readEngagementEvidence(
            String(engagementId),
            String(selectedEvidenceId),
            readMode,
            readMode === "match" ? matchQuery : "",
          ),
  });

  return (
    <>
      {showCatalog && (
        <EngagementEvidenceCatalog
          evidence={evidence}
          filters={filters}
          onFiltersChange={onFiltersChange}
          onPreviewEvidence={(evidenceId) => {
            onSelectEvidence(evidenceId);
            onOpenChange(true);
          }}
          isLoading={isLoading}
          isError={isError}
          errorMessage={errorMessage}
          emptyMessage={emptyMessage}
        />
      )}

      <Sheet open={isOpen} onOpenChange={onOpenChange}>
        <SheetContent
          side="right"
          className="w-[720px] max-w-[90vw] border-slate-800/80 bg-slate-950/95 text-slate-100 backdrop-blur-sm"
        >
          <SheetHeader>
            <SheetTitle className="text-slate-100">Evidence Preview</SheetTitle>
            <SheetDescription className="text-slate-400">
              {useKnowledgeApi
                ? "User-scoped bounded evidence read."
                : "Engagement-owned bounded evidence read."}
            </SheetDescription>
          </SheetHeader>

          {!selectedEvidenceId ? (
            <div className="mt-4 text-sm text-slate-300">Select evidence to preview.</div>
          ) : (
            <div className="mt-4 space-y-3">
              <div className={`${engagementDetailSectionClass} text-xs text-slate-300`}>
                <p>Evidence ID: {selectedEvidenceId}</p>
                <p>Storage Mode: {selectedEvidence?.storage_mode || "-"}</p>
                <p>Source Tool: {selectedEvidence?.source_tool || "-"}</p>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                <Select value={readMode} onValueChange={(v) => setReadMode(v as EvidenceReadMode)}>
                  <SelectTrigger aria-label="Evidence Read Mode" className={engagementSelectTriggerClass}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="head">Head</SelectItem>
                    <SelectItem value="tail">Tail</SelectItem>
                    <SelectItem value="match">Match</SelectItem>
                    <SelectItem value="full">Full</SelectItem>
                    <SelectItem value="auto">Auto</SelectItem>
                  </SelectContent>
                </Select>
                <Input
                  aria-label="Evidence Match Query"
                  placeholder="Match query"
                  value={matchQuery}
                  onChange={(event) => setMatchQuery(event.target.value)}
                  disabled={readMode !== "match"}
                  className={`md:col-span-2 ${engagementInputClass}`}
                />
              </div>

              {previewQuery.isLoading ? (
                <div className={`${engagementDetailSectionClass} text-sm text-slate-300`}>
                  Loading evidence preview...
                </div>
              ) : previewQuery.isError ? (
                <div className="rounded-lg border border-red-900/80 bg-red-950/20 p-3 text-sm text-red-300">
                  Failed to read evidence preview.
                </div>
              ) : previewQuery.data?.status !== "ready" ? (
                <div className={`${engagementDetailSectionClass} text-sm text-slate-300`}>
                  {previewQuery.data?.status === "not_available"
                    ? "Evidence content is metadata-only or unavailable."
                    : useKnowledgeApi
                      ? "Evidence not found."
                      : "Evidence not found for this engagement."}
                </div>
              ) : (
                <div className="rounded-lg border border-slate-800/90 bg-slate-950/80 p-3">
                  <div className="mb-2 flex items-center gap-2 text-xs text-slate-400">
                    <span>mode: {previewQuery.data.mode_used}</span>
                    {previewQuery.data.truncated && (
                      <EngagementIndicatorBadge>
                        truncated
                      </EngagementIndicatorBadge>
                    )}
                  </div>
                  <ToolCardTerminalOutput
                    outputText={previewQuery.data.content || ""}
                    isExpanded
                    isReady
                    testId="engagement-evidence-preview"
                  />
                </div>
              )}
            </div>
          )}
        </SheetContent>
      </Sheet>
    </>
  );
}
