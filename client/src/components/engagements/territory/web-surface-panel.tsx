/* Service-bound web-surface panel that renders origin summaries and expandable path rows. */

import { useEffect, useMemo, useState } from "react";

import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import { engagementDetailSectionClass, engagementInlineButtonClass } from "@/components/engagements/engagement-ui";
import { isWebSurfaceService } from "@/components/engagements/service-presentation";
import {
  useEngagementWebSurfaceOrigins,
  useEngagementWebSurfacePathPage,
} from "@/hooks/use-engagement-knowledge";
import type { GraphNode } from "@/types/engagement-knowledge";

interface WebSurfacePanelProps {
  engagementId: string | number | null | undefined;
  selectedNode: GraphNode | null;
}

function isServiceNode(node: GraphNode): boolean {
  return node.node_type === "service" || (node.subject_key || "").startsWith("service.");
}

function isWebSurfaceGraphNode(node: GraphNode): boolean {
  if (!isServiceNode(node)) {
    return false;
  }
  const metadata = node.metadata || {};
  return isWebSurfaceService({
    id: node.id,
    service_key: node.subject_key,
    service_name: typeof metadata.service_name === "string" ? metadata.service_name : node.label,
    application_protocol: typeof metadata.application_protocol === "string" ? metadata.application_protocol : null,
    protocol: typeof metadata.protocol === "string" ? metadata.protocol : null,
    transport_protocol: typeof metadata.transport_protocol === "string" ? metadata.transport_protocol : null,
    port: typeof metadata.port === "number" ? metadata.port : null,
    metadata,
  });
}

function formatByteSize(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "n/a";
  }
  const size = Number(value);
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function ProducerBadge({ producer }: { producer: string }) {
  return (
    <EngagementIndicatorBadge size="xs" className="uppercase tracking-wide">
      {producer}
    </EngagementIndicatorBadge>
  );
}

export function WebSurfacePanel({ engagementId, selectedNode }: WebSurfacePanelProps) {
  const [expandedOriginKey, setExpandedOriginKey] = useState<string | null>(null);
  const [includeNoisy, setIncludeNoisy] = useState(false);

  const serviceKey = selectedNode?.id || null;
  const eligible = Boolean(selectedNode && isWebSurfaceGraphNode(selectedNode) && engagementId && serviceKey);

  useEffect(() => {
    setExpandedOriginKey(null);
  }, [serviceKey, engagementId]);

  const originsQuery = useEngagementWebSurfaceOrigins(
    engagementId,
    eligible ? serviceKey : null,
    { include_noisy: includeNoisy },
  );
  const pathsQuery = useEngagementWebSurfacePathPage(
    engagementId,
    eligible && expandedOriginKey ? serviceKey : null,
    {
      origin_key: expandedOriginKey || undefined,
      include_noisy: includeNoisy,
      limit: 100,
      offset: 0,
    },
  );

  const summary = useMemo(() => {
    const items = originsQuery.data?.items || [];
    const producerSet = new Set<string>();
    let totalPaths = 0;
    let visiblePaths = 0;
    let hiddenNoisy = 0;
    let calibratedWarnings = 0;
    for (const origin of items) {
      totalPaths += origin.total_paths || 0;
      visiblePaths += origin.visible_paths || 0;
      hiddenNoisy += origin.hidden_noisy || 0;
      calibratedWarnings += origin.calibrated_warnings || 0;
      for (const producer of origin.producers || []) {
        if (producer) {
          producerSet.add(producer);
        }
      }
    }
    return {
      originCount: items.length,
      totalPaths,
      visiblePaths,
      hiddenNoisy,
      calibratedWarnings,
      producers: Array.from(producerSet).sort(),
    };
  }, [originsQuery.data?.items]);

  if (!eligible) {
    return null;
  }

  return (
    <section className={engagementDetailSectionClass} data-testid="web-surface-panel">
      <div className="flex items-center justify-between gap-2">
        <p className="text-slate-200">Web Surface</p>
        <button
          type="button"
          className={engagementInlineButtonClass}
          onClick={() => setExpandedOriginKey(null)}
        >
          Collapse All
        </button>
      </div>

      {originsQuery.isLoading ? (
        <p className="text-slate-400" data-testid="web-surface-summary-loading">
          Loading web-surface summary...
        </p>
      ) : originsQuery.isError ? (
        <p className="text-red-300" data-testid="web-surface-summary-error">
          Failed to load web-surface summary.
        </p>
      ) : (
        <div className="space-y-1 text-[11px] text-slate-300" data-testid="web-surface-summary">
          <p>Origins: {summary.originCount}</p>
          <p>Visible paths: {summary.visiblePaths}</p>
          <p>Total paths: {summary.totalPaths}</p>
          <p>Calibrated warnings: {summary.calibratedWarnings}</p>
          {!includeNoisy && summary.hiddenNoisy > 0 && <p>Hidden noisy paths: {summary.hiddenNoisy}</p>}
          <div className="flex flex-wrap gap-1 pt-1">
            {summary.producers.length > 0 ? (
              summary.producers.map((producer) => (
                <ProducerBadge key={producer} producer={producer} />
              ))
            ) : (
              <span className="text-slate-500">No producers yet</span>
            )}
          </div>
        </div>
      )}

      <label className="flex items-center gap-2 text-[11px] text-slate-300">
        <input
          type="checkbox"
          checked={includeNoisy}
          onChange={(event) => setIncludeNoisy(event.currentTarget.checked)}
        />
        Include noisy paths
      </label>

      <div className="space-y-1 text-[11px] text-slate-300" data-testid="web-surface-origins">
        {(originsQuery.data?.items || []).length === 0 ? (
          <p>No origin summaries available.</p>
        ) : (
          (originsQuery.data?.items || []).map((origin) => {
            const isExpanded = expandedOriginKey === origin.origin_key;
            return (
              <div
                key={origin.origin_key}
                className="rounded border border-slate-700/70 bg-slate-950/70 px-2 py-1"
              >
                <div className="flex items-center justify-between gap-2">
                  <p className="truncate text-slate-200" title={origin.origin_key}>
                    {origin.origin_key}
                  </p>
                  <button
                    type="button"
                    className={engagementInlineButtonClass}
                    onClick={() =>
                      setExpandedOriginKey((previous) =>
                        previous === origin.origin_key ? null : origin.origin_key,
                      )
                    }
                  >
                    {isExpanded ? "Hide Paths" : "Show Paths"}
                  </button>
                </div>
                <p className="text-slate-400">
                  Visible: {origin.visible_paths} / Total: {origin.total_paths}
                  {origin.calibrated_warnings > 0 ? ` | Calibrated: ${origin.calibrated_warnings}` : ""}
                </p>

                {isExpanded && (
                  <div className="mt-2 space-y-1" data-testid="web-surface-paths">
                    {pathsQuery.isLoading ? (
                      <p>Loading paths...</p>
                    ) : pathsQuery.isError ? (
                      <p className="text-red-300">Failed to load paths.</p>
                    ) : pathsQuery.data?.items.length ? (
                      pathsQuery.data.items.map((item) => {
                        const producers = Object.keys(item.producers || {}).sort();
                        return (
                          <div
                            key={item.canonical_url}
                            className="rounded border border-slate-700/70 bg-slate-900/65 px-2 py-1"
                          >
                            <p className="truncate text-slate-200" title={item.canonical_url}>
                              {item.path || item.canonical_url}
                            </p>
                            <p className="text-slate-400">
                              Status: {item.last_status_code ?? "n/a"} | Size: {formatByteSize(item.last_response_size)}
                            </p>
                            <div className="mt-1 flex flex-wrap items-center gap-1">
                              {producers.length ? (
                                producers.map((producer) => (
                                  <ProducerBadge key={`${item.canonical_url}:${producer}`} producer={producer} />
                                ))
                              ) : (
                                <span className="text-slate-500">No producers</span>
                              )}
                              {item.calibrated_baseline && (
                                <EngagementIndicatorBadge size="xs" className="uppercase tracking-wide">
                                  Calibrated
                                </EngagementIndicatorBadge>
                              )}
                            </div>
                          </div>
                        );
                      })
                    ) : (
                      <p>No path rows available.</p>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
