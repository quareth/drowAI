/* Asset detail panel with linked services, findings, and evidence entry points. */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import {
  engagementCardClass,
  engagementDetailSectionClass,
  engagementInlineButtonClass,
} from "@/components/engagements/engagement-ui";
import { EngagementServiceSummary } from "@/components/engagements/engagement-service-summary";
import type { AssetDetail } from "@/types/engagement-knowledge";
import type { RichHostState } from "@/types/knowledge";
import { getMetadataState } from "@/types/knowledge";

interface EngagementAssetDetailPanelProps {
  asset?: AssetDetail;
  isLoading?: boolean;
  errorMessage?: string | null;
  onPreviewEvidence?: (evidenceId: string) => void;
}

function summarizeEvidenceCount(asset: AssetDetail): number {
  return asset.findings.reduce((sum, finding) => sum + finding.evidence_count, 0);
}

export function EngagementAssetDetailPanel({
  asset,
  isLoading = false,
  errorMessage,
  onPreviewEvidence,
}: EngagementAssetDetailPanelProps) {
  if (isLoading) {
    return (
      <Card className={`${engagementCardClass} h-full`}>
        <CardContent className="p-6 text-sm text-slate-300">Loading asset details...</CardContent>
      </Card>
    );
  }

  if (!asset) {
    return (
      <Card className={`${engagementCardClass} h-full`}>
        <CardContent className="p-6 text-sm text-slate-300">
          {errorMessage || "Select an asset to inspect linked services, findings, and evidence."}
        </CardContent>
      </Card>
    );
  }

  const evidenceCount = summarizeEvidenceCount(asset);

  return (
    <Card className={`${engagementCardClass} h-full`}>
      <CardHeader>
        <CardTitle className="text-base font-semibold text-white">
          {asset.display_name || asset.hostname || asset.ip_address || asset.asset_key || asset.id}
        </CardTitle>
        <div className="flex flex-wrap gap-1.5 pt-1">
          <EngagementIndicatorBadge>
            {asset.asset_type || "unknown"}
          </EngagementIndicatorBadge>
          <EngagementIndicatorBadge>
            findings: {asset.finding_count}
          </EngagementIndicatorBadge>
          <EngagementIndicatorBadge>
            services: {asset.service_count}
          </EngagementIndicatorBadge>
          <EngagementIndicatorBadge>
            evidence refs: {evidenceCount}
          </EngagementIndicatorBadge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 text-xs text-slate-300">
        <section>
          <h3 className="mb-1 font-medium text-slate-100">Risk State</h3>
          <div className={`${engagementDetailSectionClass} space-y-1`}>
            <p>Vulnerable: {asset.is_vulnerable ? "Yes" : "No"}</p>
            <p>Exploited: {asset.is_exploited ? "Yes" : "No"}</p>
            <p>Status: {asset.status || "-"}</p>
            <p>Last Seen: {asset.last_seen_at || "-"}</p>
          </div>
        </section>

        {(() => {
          const hostState = getMetadataState<RichHostState>(asset.metadata);
          if (!hostState) return null;
          const hasRichData =
            hostState.os_top_guess ||
            (hostState.hostnames && hostState.hostnames.length > 0) ||
            (hostState.host_script_summaries && hostState.host_script_summaries.length > 0);
          if (!hasRichData) return null;
          return (
            <section>
              <h3 className="mb-1 font-medium text-slate-100">Host Intelligence</h3>
              <div className={`${engagementDetailSectionClass} space-y-1`}>
                {hostState.hostnames && hostState.hostnames.length > 0 && (
                  <p>Hostnames: {hostState.hostnames.join(", ")}</p>
                )}
                {hostState.os_top_guess && <p>OS: {hostState.os_top_guess}</p>}
                {hostState.os_matches && hostState.os_matches.length > 1 && (
                  <p className="text-slate-400">
                    Other OS guesses: {hostState.os_matches.slice(1).map((m) => `${m.name} (${m.accuracy ?? "?"}%)`).join(", ")}
                  </p>
                )}
                {hostState.host_script_summaries && hostState.host_script_summaries.length > 0 && (
                  <div>
                    <p className="text-slate-400 mb-0.5">Host scripts:</p>
                    {hostState.host_script_summaries.map((s) => (
                      <p key={s.script_id} className="text-slate-400 pl-2 truncate" title={s.summary}>
                        {s.script_id}: {s.summary || "(no output)"}
                      </p>
                    ))}
                  </div>
                )}
                {hostState.trace_summary && (
                  <p className="text-slate-400">
                    Traceroute: {hostState.trace_summary.hop_count} hop{hostState.trace_summary.hop_count !== 1 ? "s" : ""}
                  </p>
                )}
              </div>
            </section>
          );
        })()}

        <section>
          <h3 className="mb-1 font-medium text-slate-100">Linked Services</h3>
          {asset.services.length === 0 ? (
            <p className="text-slate-400">No linked services.</p>
          ) : (
            <div className="space-y-1">
              {asset.services.map((service) => (
                <EngagementServiceSummary key={service.id} service={service} />
              ))}
            </div>
          )}
        </section>

        <section>
          <h3 className="mb-1 font-medium text-slate-100">Linked Findings</h3>
          {asset.findings.length === 0 ? (
            <p className="text-slate-400">No linked findings.</p>
          ) : (
            <div className="space-y-1">
              {asset.findings.map((finding) => (
                <div
                  key={finding.id}
                  className={`${engagementDetailSectionClass} flex items-center justify-between`}
                >
                  <span className="text-slate-200">
                    {finding.title || finding.finding_key || finding.id}
                  </span>
                  <div className="flex items-center gap-2">
                    <span className="text-slate-400">evidence: {finding.evidence_count}</span>
                    {typeof finding.evidence_refs?.[0]?.evidence_archive_id === "string" && (
                      <button
                        type="button"
                        onClick={() =>
                          onPreviewEvidence?.(String(finding.evidence_refs[0].evidence_archive_id))
                        }
                        className={engagementInlineButtonClass}
                      >
                        Preview
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
