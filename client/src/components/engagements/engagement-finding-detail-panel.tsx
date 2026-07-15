/* Finding detail side panel for linked asset/service context and evidence lineage. */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import {
  FindingSeverityBadge,
  FindingStatusBadge,
} from "@/components/engagements/finding-badges";
import {
  engagementCardClass,
  engagementDetailSectionClass,
  engagementInlineButtonClass,
} from "@/components/engagements/engagement-ui";
import { EngagementServiceSummary } from "@/components/engagements/engagement-service-summary";
import type { FindingDetail } from "@/types/engagement-knowledge";
import type {
  RichNucleiFindingState,
  RichNucleiFindingDetails,
} from "@/types/knowledge";
import {
  getMetadataState,
  getMetadataRichDetails,
} from "@/types/knowledge";

interface EngagementFindingDetailPanelProps {
  finding?: FindingDetail;
  isLoading?: boolean;
  errorMessage?: string | null;
  onPreviewEvidence?: (evidenceId: string) => void;
  onOpenAsset?: (assetId: string) => void;
}

function extractSourceTool(finding: FindingDetail | undefined): string {
  if (!finding) {
    return "unknown";
  }
  const sourceFromList = finding.source_tool;
  if (sourceFromList) {
    return sourceFromList;
  }
  const metadataSource = finding.metadata?.source_tool;
  if (typeof metadataSource === "string" && metadataSource.trim()) {
    return metadataSource.trim();
  }
  return "unknown";
}

export function EngagementFindingDetailPanel({
  finding,
  isLoading = false,
  errorMessage,
  onPreviewEvidence,
  onOpenAsset,
}: EngagementFindingDetailPanelProps) {
  if (isLoading) {
    return (
      <Card className={`${engagementCardClass} h-full`}>
        <CardContent className="p-6 text-sm text-slate-300">Loading finding details...</CardContent>
      </Card>
    );
  }

  if (!finding) {
    return (
      <Card className={`${engagementCardClass} h-full`}>
        <CardContent className="p-6 text-sm text-slate-300">
          {errorMessage ||
            "Select a finding to inspect asset, service, evidence, and lineage details."}
        </CardContent>
      </Card>
    );
  }

  const sourceTool = extractSourceTool(finding);
  const linkedAsset = finding.asset;

  return (
    <Card className={`${engagementCardClass} h-full`}>
      <CardHeader>
        <CardTitle className="text-base font-semibold text-white">
          {finding.title || finding.finding_key || "Finding detail"}
        </CardTitle>
        <div className="flex flex-wrap gap-1.5 pt-1">
          <FindingSeverityBadge severity={finding.severity} />
          <FindingStatusBadge
            isExploited={finding.is_exploited}
            status={finding.status}
          />
          <EngagementIndicatorBadge>
            source: {sourceTool}
          </EngagementIndicatorBadge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 text-xs text-slate-300">
        <section>
          <h3 className="mb-1 font-medium text-slate-100">Linked Asset</h3>
          {linkedAsset ? (
            <div className={engagementDetailSectionClass}>
              <p>{linkedAsset.display_name || linkedAsset.asset_key || linkedAsset.id}</p>
              <p className="text-slate-400 mt-1">
                {linkedAsset.asset_type || "unknown"} {linkedAsset.ip_address || ""}
              </p>
              <button
                type="button"
                onClick={() => onOpenAsset?.(linkedAsset.id)}
                className={`mt-2 ${engagementInlineButtonClass}`}
              >
                Open in Assets
              </button>
            </div>
          ) : (
            <p className="text-slate-400">No linked asset.</p>
          )}
        </section>

        <section>
          <h3 className="mb-1 font-medium text-slate-100">Linked Service</h3>
          {finding.service ? (
            <EngagementServiceSummary service={finding.service} />
          ) : (
            <p className="text-slate-400">No linked service.</p>
          )}
        </section>

        {(() => {
          const state = getMetadataState<RichNucleiFindingState>(
            finding.metadata as Record<string, unknown> | undefined,
          );
          const richDetails = getMetadataRichDetails<RichNucleiFindingDetails>(
            finding.metadata as Record<string, unknown> | undefined,
          );
          const detectorId =
            typeof state?.detector_id === "string" ? state.detector_id : null;
          const scriptId =
            typeof (state as Record<string, unknown> | undefined)?.script_id === "string"
              ? (state as Record<string, unknown>).script_id as string
              : null;
          const summary =
            typeof (state as Record<string, unknown> | undefined)?.summary === "string"
              ? (state as Record<string, unknown>).summary as string
              : null;
          const descriptionSummary = state?.description_summary || null;
          const matcherId = state?.matcher_id || null;
          const matchedAt = state?.matched_at || null;
          const classification = richDetails?.classification;
          const tags = richDetails?.tags;
          const references = richDetails?.references;
          const extractedResults = richDetails?.extracted_results;
          const hasDetectionInfo = detectorId || scriptId || summary || matcherId;
          const hasClassification = classification && (classification.cve_ids?.length || classification.cwe_ids?.length);
          const hasRichDetail = hasDetectionInfo || descriptionSummary || hasClassification || tags?.length || references?.length || extractedResults?.length || matchedAt;
          if (!hasRichDetail) return null;
          const metadataSourceTool =
            typeof finding.metadata?.source_tool === "string" ? finding.metadata.source_tool : null;
          const isNmapDerived =
            finding.source_tool === "nmap" ||
            metadataSourceTool === "nmap" ||
            detectorId?.startsWith("nmap/") === true;
          return (
            <>
              {hasDetectionInfo && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-100">Detection Rule</h3>
                  <div className={`${engagementDetailSectionClass} space-y-1`}>
                    {isNmapDerived && <p className="text-slate-400">Source: nmap deterministic script detection</p>}
                    {detectorId && <p>Rule: {detectorId}</p>}
                    {matcherId && <p>Matcher: {matcherId}</p>}
                    {scriptId && <p>Script: {scriptId}</p>}
                    {summary && <p className="text-slate-400 truncate" title={summary}>{summary}</p>}
                  </div>
                </section>
              )}

              {descriptionSummary && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-100">Description</h3>
                  <div className={engagementDetailSectionClass}>
                    <p className="text-slate-300 whitespace-pre-wrap">{descriptionSummary}</p>
                  </div>
                </section>
              )}

              {hasClassification && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-100">Classification</h3>
                  <div className={`${engagementDetailSectionClass} space-y-1`}>
                    {classification?.cve_ids?.length ? (
                      <div className="flex flex-wrap gap-1">
                        {classification.cve_ids.map((cve) => (
                          <EngagementIndicatorBadge key={cve}>
                            {cve}
                          </EngagementIndicatorBadge>
                        ))}
                      </div>
                    ) : null}
                    {classification?.cwe_ids?.length ? (
                      <div className="flex flex-wrap gap-1">
                        {classification.cwe_ids.map((cwe) => (
                          <EngagementIndicatorBadge key={cwe}>
                            {cwe}
                          </EngagementIndicatorBadge>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </section>
              )}

              {tags && tags.length > 0 && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-100">Tags</h3>
                  <div className={engagementDetailSectionClass}>
                    <div className="flex flex-wrap gap-1">
                      {tags.map((tag) => (
                        <EngagementIndicatorBadge key={tag}>
                          {tag}
                        </EngagementIndicatorBadge>
                      ))}
                    </div>
                  </div>
                </section>
              )}

              {references && references.length > 0 && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-100">References</h3>
                  <div className={`${engagementDetailSectionClass} space-y-1`}>
                    {references.map((ref, idx) => (
                      <p key={idx} className="text-blue-400 truncate" title={ref}>{ref}</p>
                    ))}
                  </div>
                </section>
              )}

              {matchedAt && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-100">Matched Target</h3>
                  <div className={engagementDetailSectionClass}>
                    <p className="text-slate-200 break-all">{matchedAt}</p>
                  </div>
                </section>
              )}

              {extractedResults && extractedResults.length > 0 && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-100">Extracted Results</h3>
                  <div className={`${engagementDetailSectionClass} space-y-1`}>
                    {extractedResults.map((result, idx) => (
                      <p key={idx} className="text-slate-300 font-mono text-[11px]">{result}</p>
                    ))}
                  </div>
                </section>
              )}
            </>
          );
        })()}

        <section>
          <h3 className="mb-1 font-medium text-slate-100">Evidence References</h3>
          <div className="space-y-1">
            {finding.evidence_refs.length === 0 ? (
              <p className="text-slate-400">No linked evidence references.</p>
            ) : (
              finding.evidence_refs.map((ref, index) => {
                const evidenceId = ref.evidence_archive_id;
                return (
                  <div
                    key={`${evidenceId}-${index}`}
                    className={`${engagementDetailSectionClass} flex items-center justify-between`}
                  >
                    <span className="text-slate-200">{evidenceId}</span>
                    <button
                      type="button"
                      onClick={() => onPreviewEvidence?.(evidenceId)}
                      className={engagementInlineButtonClass}
                    >
                      Preview
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </section>

        <section>
          <h3 className="mb-1 font-medium text-slate-100">Provenance Lineage</h3>
          <div className={`${engagementDetailSectionClass} space-y-1`}>
            <p>Finding Key: {finding.finding_key || "-"}</p>
            <p>Subject: {finding.subject_key || "-"}</p>
            <p>Assertion Level: {finding.assertion_level || "-"}</p>
            <p>Confidence: {finding.confidence || "-"}</p>
            <p>First Seen: {finding.first_seen_at || "-"}</p>
            <p>Last Seen: {finding.last_seen_at || "-"}</p>
          </div>
        </section>
      </CardContent>
    </Card>
  );
}
