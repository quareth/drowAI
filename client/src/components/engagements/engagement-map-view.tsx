/* Map workspace view for engagement graph exploration and linked context actions. */

import { EngagementMapPanel } from "@/components/engagements/engagement-map-panel";
import { Card, CardContent } from "@/components/ui/card";
import type {
  EngagementGraphSnapshot,
  GraphEdge,
  GraphNode,
} from "@/types/engagement-knowledge";

interface EngagementMapViewProps {
  graph?: EngagementGraphSnapshot;
  isLoading?: boolean;
  errorMessage?: string | null;
  onSelectNode: (node: GraphNode) => void;
  onSelectEdge: (edge: GraphEdge) => void;
}

export function EngagementMapView({
  graph,
  isLoading = false,
  errorMessage = null,
  onSelectNode,
  onSelectEdge,
}: EngagementMapViewProps) {
  if (errorMessage) {
    return (
      <Card className="rounded-xl border-red-900/80 bg-red-950/20">
        <CardContent className="p-6 text-red-300">{errorMessage}</CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      <EngagementMapPanel
        graph={graph}
        isLoading={isLoading}
        onSelectNode={onSelectNode}
        onSelectEdge={onSelectEdge}
      />
    </div>
  );
}
