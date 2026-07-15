/* Engagement territory topology surface backed by graph adapter + interactive canvas module. */

import { useMemo, useState } from "react";

import {
  adaptEngagementGraphToTopology,
  AssetInspectorPanel,
  TopologyCanvas,
} from "@/components/engagements/territory";
import { Card, CardContent } from "@/components/ui/card";
import { engagementCardClass } from "@/components/engagements/engagement-ui";
import type { TopologyNode } from "@/components/engagements/territory";
import type { EngagementGraphSnapshot, GraphEdge, GraphNode } from "@/types/engagement-knowledge";

export interface MapSelection {
  node: GraphNode | null;
  edge: GraphEdge | null;
}

interface EngagementMapPanelProps {
  graph?: EngagementGraphSnapshot;
  isLoading?: boolean;
  onSelectNode?: (node: GraphNode) => void;
  onSelectEdge?: (edge: GraphEdge) => void;
}

export function EngagementMapPanel({
  graph,
  isLoading = false,
  onSelectNode,
  onSelectEdge,
}: EngagementMapPanelProps) {
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const topologyView = useMemo(() => adaptEngagementGraphToTopology(graph), [graph]);
  const topology = topologyView.graph;
  const assetNodes = useMemo(
    () => topology.nodes.filter((node) => node.kind === "asset"),
    [topology.nodes],
  );
  const selectedAsset = useMemo(() => {
    if (assetNodes.length === 0) {
      return null;
    }
    const explicit = selectedAssetId
      ? assetNodes.find((node) => node.id === selectedAssetId)
      : null;
    if (explicit) {
      return explicit;
    }
    return [...assetNodes].sort((a, b) => {
      const findingDelta = b.childFindings.length - a.childFindings.length;
      if (findingDelta !== 0) {
        return findingDelta;
      }
      const serviceDelta = b.childServices.length - a.childServices.length;
      if (serviceDelta !== 0) {
        return serviceDelta;
      }
      return a.label.localeCompare(b.label);
    })[0];
  }, [assetNodes, selectedAssetId]);

  const findAssetForGraphNode = (node: GraphNode): TopologyNode | null => {
    const direct = assetNodes.find((asset) => asset.id === node.id);
    if (direct) {
      return direct;
    }
    return assetNodes.find((asset) =>
      asset.childServices.some((service) => service.id === node.id) ||
      asset.childFindings.some((finding) => finding.id === node.id),
    ) || null;
  };

  const handleSelectTopologyNode = (node: TopologyNode) => {
    if (node.kind === "asset") {
      setSelectedAssetId(node.id);
    }
  };

  const handleSelectNode = (node: GraphNode) => {
    const relatedAsset = findAssetForGraphNode(node);
    if (relatedAsset) {
      setSelectedAssetId(relatedAsset.id);
    }
    onSelectNode?.(node);
  };

  const handleSelectInspectorService = (serviceId: string) => {
    const service = selectedAsset?.childServices.find((item) => item.id === serviceId);
    if (!service) {
      return;
    }
    onSelectNode?.(service.sourceNode || {
      id: service.id,
      subject_key: service.id,
      node_type: "service",
      label: service.label,
      metadata: service.metadata,
    });
  };

  if (isLoading) {
    return (
      <Card className={engagementCardClass}>
        <CardContent className="p-6 text-sm text-slate-300">Loading relationship map...</CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-slate-700/70 bg-slate-900/65 px-3 py-2 text-xs text-slate-300">
        Territory topology preview: interactive, zoomable, read-only network map.
      </div>
      {topology.nodes.length === 0 ? (
        <Card className={engagementCardClass}>
          <CardContent className="space-y-2 p-6">
            <p className="text-sm font-medium text-slate-200">
              No durable territory graph data is available for this engagement yet.
            </p>
            <p className="text-xs text-slate-400">
              Run tools that project assets, services, findings, or relationships into knowledge.
              Empty territory now means the backend has not produced graph records.
            </p>
          </CardContent>
        </Card>
      ) : null}
      {topology.nodes.length > 0 ? (
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
          <TopologyCanvas
            graph={topology}
            onSelectNode={handleSelectNode}
            onSelectEdge={onSelectEdge}
            onSelectTopologyNode={handleSelectTopologyNode}
          />
          <AssetInspectorPanel
            selectedAsset={selectedAsset}
            onSelectService={handleSelectInspectorService}
          />
        </div>
      ) : null}
    </div>
  );
}
