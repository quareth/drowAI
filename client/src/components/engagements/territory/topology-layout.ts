/* Worker-backed ELK layout service with deterministic fallback for territory topology. */

import ELK from "elkjs/lib/elk-api.js";
import ELKWorker from "elkjs/lib/elk-worker.min.js?worker";

import type {
  TopologyLayoutInput,
  TopologyLayoutPosition,
} from "@/components/engagements/territory/topology-types";
import { getTopologyNodeDimensions } from "@/components/engagements/territory/topology-presentation";

type TopologyLayoutEngine = InstanceType<typeof ELK>;

let elk: TopologyLayoutEngine | null | undefined;

function getTopologyLayoutEngine(): TopologyLayoutEngine | null {
  if (elk !== undefined) {
    return elk;
  }

  try {
    elk = new ELK({ workerFactory: () => new ELKWorker() });
  } catch {
    elk = null;
  }
  return elk;
}

function fallbackGrid(
  nodes: TopologyLayoutInput["nodes"],
): Record<string, TopologyLayoutPosition> {
  const result: Record<string, TopologyLayoutPosition> = {};
  const columns = 4;
  const colWidth = 280;
  const rowHeight = 150;
  nodes.forEach((node, index) => {
    result[node.id] = {
      x: (index % columns) * colWidth,
      y: Math.floor(index / columns) * rowHeight,
    };
  });
  return result;
}

export async function computeTopologyLayout(
  input: TopologyLayoutInput,
): Promise<Record<string, TopologyLayoutPosition>> {
  if (input.nodes.length === 0) {
    return {};
  }

  const layoutEngine = getTopologyLayoutEngine();
  if (layoutEngine === null) {
    return fallbackGrid(input.nodes);
  }

  try {
    const layout = await layoutEngine.layout({
      id: "territory-root",
      layoutOptions: {
        "elk.algorithm": "layered",
        "elk.direction": "RIGHT",
        "elk.layered.spacing.nodeNodeBetweenLayers": "100",
        "elk.spacing.nodeNode": "80",
        "elk.edgeRouting": "SPLINES",
        "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      },
      children: input.nodes.map((node) => {
        const size = getTopologyNodeDimensions(node);
        return {
          id: node.id,
          width: size.width,
          height: size.height,
        };
      }),
      edges: input.edges.map((edge) => ({
        id: edge.id,
        sources: [edge.source],
        targets: [edge.target],
      })),
    });

    const result: Record<string, TopologyLayoutPosition> = {};
    for (const child of layout.children || []) {
      result[child.id] = {
        x: child.x ?? 0,
        y: child.y ?? 0,
      };
    }
    return result;
  } catch {
    return fallbackGrid(input.nodes);
  }
}

export function prewarmTopologyLayout(): void {
  getTopologyLayoutEngine();
}
