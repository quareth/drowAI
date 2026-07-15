/* Side-panel inspector for full details behind compact Territory asset nodes. */

import { AlertTriangle, Server, Waypoints } from "lucide-react";

import { EngagementIndicatorBadge } from "@/components/engagements/engagement-indicator-badge";
import { ServiceChip } from "@/components/engagements/territory/service-chip";
import {
  formatSeverity,
  severityIndicatorTone,
  TOPOLOGY_CANVAS_HEIGHT,
} from "@/components/engagements/territory/topology-presentation";
import type {
  TopologyFindingBadge,
  TopologyNode,
} from "@/components/engagements/territory/topology-types";

interface AssetInspectorPanelProps {
  selectedAsset: TopologyNode | null;
  onSelectService?: (serviceId: string) => void;
}

function FindingRow({ finding }: { finding: TopologyFindingBadge }) {
  return (
    <div
      className="rounded-md border border-slate-700/70 bg-slate-950/45 px-2 py-1.5"
      data-testid={`asset-inspector-finding-${finding.id}`}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[11px] font-medium text-slate-100" title={finding.label}>
            {finding.label}
          </p>
          <div className="mt-1 flex flex-wrap gap-1">
            <EngagementIndicatorBadge
              size="xs"
              tone={severityIndicatorTone(finding.severity)}
            >
              {formatSeverity(finding.severity)}
            </EngagementIndicatorBadge>
            {finding.status && (
              <EngagementIndicatorBadge size="xs">
                {finding.status}
              </EngagementIndicatorBadge>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function AssetInspectorPanel({
  selectedAsset,
  onSelectService,
}: AssetInspectorPanelProps) {
  if (!selectedAsset || selectedAsset.kind !== "asset") {
    return (
      <aside
        className="rounded-xl border border-slate-700/80 bg-slate-950/75 p-4 text-xs text-slate-400"
        style={{ height: TOPOLOGY_CANVAS_HEIGHT }}
        data-testid="territory-asset-inspector"
      >
        <p className="text-sm font-medium text-slate-200">Asset Inspector</p>
        <p className="mt-2">
          Select an asset to inspect all services and findings without expanding the topology node.
        </p>
      </aside>
    );
  }

  return (
    <aside
      className="flex overflow-hidden rounded-xl border border-slate-700/80 bg-slate-950/75 text-xs text-slate-300"
      style={{ height: TOPOLOGY_CANVAS_HEIGHT }}
      data-testid="territory-asset-inspector"
    >
      <div className="flex min-h-0 w-full flex-col">
        <div className="shrink-0 border-b border-slate-800/80 px-4 py-3">
          <div className="flex items-start gap-2">
            <Server className="mt-0.5 h-4 w-4 shrink-0 text-slate-300" />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-slate-100" title={selectedAsset.label}>
                {selectedAsset.label}
              </p>
              <p className="mt-1 text-[10px] uppercase tracking-wide text-slate-500">Selected asset</p>
            </div>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <div className="rounded-md border border-slate-800 bg-slate-900/55 px-2 py-1.5">
              <p className="text-[10px] uppercase tracking-wide text-slate-500">Services</p>
              <p className="text-lg font-semibold text-slate-100">{selectedAsset.childServices.length}</p>
            </div>
            <div className="rounded-md border border-slate-800 bg-slate-900/55 px-2 py-1.5">
              <p className="text-[10px] uppercase tracking-wide text-slate-500">Findings</p>
              <p className="text-lg font-semibold text-slate-100">{selectedAsset.childFindings.length}</p>
            </div>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <section>
            <div className="mb-2 flex items-center gap-1.5 text-slate-200">
              <AlertTriangle className="h-3.5 w-3.5 text-amber-300" />
              <p className="font-medium">Findings</p>
            </div>
            <div className="space-y-1.5">
              {selectedAsset.childFindings.length > 0 ? (
                selectedAsset.childFindings.map((finding) => (
                  <FindingRow key={finding.id} finding={finding} />
                ))
              ) : (
                <p className="rounded-md border border-slate-800 bg-slate-900/40 px-2 py-2 text-slate-500">
                  No attached findings.
                </p>
              )}
            </div>
          </section>

          <section className="mt-4">
            <div className="mb-2 flex items-center gap-1.5 text-slate-200">
              <Waypoints className="h-3.5 w-3.5 text-blue-300" />
              <p className="font-medium">Services</p>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {selectedAsset.childServices.length > 0 ? (
                selectedAsset.childServices.map((service) => (
                  <ServiceChip
                    key={service.id}
                    chip={service}
                    onClick={() => onSelectService?.(service.id)}
                  />
                ))
              ) : (
                <p className="rounded-md border border-slate-800 bg-slate-900/40 px-2 py-2 text-slate-500">
                  No attached services.
                </p>
              )}
            </div>
          </section>
        </div>
      </div>
    </aside>
  );
}
