/* Interactive read-only topology canvas with pan/zoom, layout, and group collapse controls. */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { Edge, EdgeMouseHandler, Node, NodeMouseHandler } from "@xyflow/react";
import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import { Compass, Shrink, ZoomIn } from "lucide-react";

import { Button } from "@/components/ui/button";
import { computeTopologyLayout } from "@/components/engagements/territory/topology-layout";
import { topologyEdgeTypes } from "@/components/engagements/territory/topology-edges";
import { topologyNodeTypes } from "@/components/engagements/territory/topology-nodes";
import { TOPOLOGY_CANVAS_HEIGHT } from "@/components/engagements/territory/topology-presentation";
import { resolveTopologyVisibility } from "@/components/engagements/territory/topology-visibility";
import type {
  TopologyEdgeRenderData,
  TopologyGraph,
  TopologyNode,
  TopologyNodeRenderData,
  TopologyServiceChip,
} from "@/components/engagements/territory/topology-types";
import type { GraphEdge, GraphNode } from "@/types/engagement-knowledge";

import "@xyflow/react/dist/style.css";

interface TopologyCanvasProps {
  graph: TopologyGraph;
  onSelectNode?: (node: GraphNode) => void;
  onSelectEdge?: (edge: GraphEdge) => void;
  onSelectTopologyNode?: (node: TopologyNode) => void;
}

function syntheticGraphNode(node: TopologyNode): GraphNode {
  return {
    id: node.id,
    subject_key: node.id,
    node_type: node.kind,
    label: node.label,
    metadata: node.metadata || {},
  };
}

function syntheticGraphEdge(
  edge: TopologyGraph["edges"][number],
): GraphEdge {
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    relationship_type: edge.label,
    confidence: null,
    first_seen_at: null,
    last_seen_at: null,
    metadata: edge.metadata || {},
  };
}

function mapLayoutSignature(graph: TopologyGraph, collapsedGroupIds: Set<string>): string {
  const groupSignature = [...collapsedGroupIds].sort().join("|");
  const nodeSignature = graph.nodes.map((node) => node.id).sort().join("|");
  const edgeSignature = graph.edges.map((edge) => edge.id).sort().join("|");
  return `${graph.source}:${groupSignature}:${nodeSignature}:${edgeSignature}`;
}

function TopologyCanvasFallback({
  graph,
  onSelectNode,
  onSelectTopologyNode,
}: TopologyCanvasProps) {
  const [collapsedGroupIds, setCollapsedGroupIds] = useState<Set<string>>(new Set());
  const groupIds = useMemo(() => Object.keys(graph.groups), [graph.groups]);
  const visibility = useMemo(
    () => resolveTopologyVisibility(graph, collapsedGroupIds),
    [collapsedGroupIds, graph],
  );

  return (
    <div
      className="relative w-full overflow-auto rounded-xl border border-slate-700/80 bg-slate-950/75 p-3"
      style={{ height: TOPOLOGY_CANVAS_HEIGHT }}
      data-testid="territory-topology-canvas"
    >
      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 border-slate-600/80 bg-slate-900/80 px-2 text-[11px] text-slate-200 hover:bg-slate-800"
        >
          <Compass className="mr-1 h-3 w-3" />
          Fit view
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 border-slate-600/80 bg-slate-900/80 px-2 text-[11px] text-slate-200 hover:bg-slate-800"
        >
          <ZoomIn className="mr-1 h-3 w-3" />
          Reset layout
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => setCollapsedGroupIds(new Set(groupIds))}
          className="h-7 border-slate-600/80 bg-slate-900/80 px-2 text-[11px] text-slate-200 hover:bg-slate-800"
        >
          <Shrink className="mr-1 h-3 w-3" />
          Collapse all
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => setCollapsedGroupIds(new Set())}
          className="h-7 border-slate-600/80 bg-slate-900/80 px-2 text-[11px] text-slate-200 hover:bg-slate-800"
        >
          Expand all
        </Button>
      </div>
      <div className="mb-2 text-[11px] text-slate-400">
        Topology compatibility mode (ResizeObserver unavailable)
      </div>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
        {visibility.nodes.map((node) => (
          <button
            key={node.id}
            type="button"
            data-testid={`territory-node-${node.id}`}
            onClick={() => {
              onSelectTopologyNode?.(node);
              onSelectNode?.(node.sourceNode || syntheticGraphNode(node));
            }}
            className="rounded-md border border-slate-700/80 bg-slate-900/75 px-2 py-2 text-left text-xs text-slate-200 hover:border-emerald-600/70 hover:bg-slate-800/70"
          >
            <p className="truncate font-medium">{node.label}</p>
            <p className="mt-1 text-[10px] uppercase tracking-wide text-slate-400">
              {node.kind}
            </p>
          </button>
        ))}
      </div>
      <div className="mt-3 rounded-md border border-slate-800/80 bg-slate-950/80 px-2 py-1 text-[11px] text-slate-400">
        Visible routes: {visibility.edges.length}
      </div>
    </div>
  );
}

function InnerTopologyCanvas({
  graph,
  onSelectNode,
  onSelectEdge,
  onSelectTopologyNode,
}: TopologyCanvasProps) {
  const reactFlow = useReactFlow<Node<TopologyNodeRenderData>, Edge<TopologyEdgeRenderData>>();
  const [collapsedGroupIds, setCollapsedGroupIds] = useState<Set<string>>(new Set());
  const [layoutEpoch, setLayoutEpoch] = useState(0);
  const [isLayingOut, setIsLayingOut] = useState(false);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<TopologyNodeRenderData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge<TopologyEdgeRenderData>>([]);

  const groupIds = useMemo(() => Object.keys(graph.groups), [graph.groups]);
  const visibility = useMemo(
    () => resolveTopologyVisibility(graph, collapsedGroupIds),
    [collapsedGroupIds, graph],
  );
  const signature = useMemo(
    () => mapLayoutSignature(
      {
        ...graph,
        nodes: visibility.nodes,
        edges: visibility.edges,
      },
      collapsedGroupIds,
    ),
    [collapsedGroupIds, graph, visibility.edges, visibility.nodes],
  );

  const handleToggleGroup = useCallback((groupId: string) => {
    setCollapsedGroupIds((previous) => {
      const next = new Set(previous);
      if (next.has(groupId)) {
        next.delete(groupId);
      } else {
        next.add(groupId);
      }
      return next;
    });
  }, []);

  const handleCollapseAll = useCallback(() => {
    setCollapsedGroupIds(new Set(groupIds));
  }, [groupIds]);

  const handleExpandAll = useCallback(() => {
    setCollapsedGroupIds(new Set());
  }, []);

  const handleResetLayout = useCallback(() => {
    setLayoutEpoch((value) => value + 1);
  }, []);

  const handleFitView = useCallback(() => {
    reactFlow.fitView({ padding: 0.2, duration: 240 });
  }, [reactFlow]);

  const handleSelectService = useCallback(
    (chip: TopologyServiceChip) => {
      const graphNode: GraphNode = chip.sourceNode || {
        id: chip.id,
        subject_key: chip.id,
        node_type: "service",
        label: chip.label,
        metadata: { ...chip.metadata, port: chip.port, protocol: chip.protocol, status: chip.status },
      };
      onSelectNode?.(graphNode);
    },
    [onSelectNode],
  );

  const handleSelectAsset = useCallback(
    (node: TopologyNode) => {
      onSelectTopologyNode?.(node);
      onSelectNode?.(node.sourceNode || syntheticGraphNode(node));
    },
    [onSelectNode, onSelectTopologyNode],
  );

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(async () => {
      setIsLayingOut(true);
      const positioned = await computeTopologyLayout({
        nodes: visibility.nodes,
        edges: visibility.edges,
      });
      if (!active) {
        return;
      }

      const nextNodes: Node<TopologyNodeRenderData>[] = visibility.nodes.map((node) => ({
        id: node.id,
        type: node.kind,
        draggable: true,
        data: {
          node,
          engagementId: graph.engagementId,
          memberCount: visibility.memberCounts[node.id] || 0,
          isCollapsed: collapsedGroupIds.has(node.id),
          onToggleGroup: handleToggleGroup,
          onSelectAsset: handleSelectAsset,
          onSelectService: handleSelectService,
        },
        position: positioned[node.id] || { x: 0, y: 0 },
      }));

      const nextEdges: Edge<TopologyEdgeRenderData>[] = visibility.edges.map((edge) => ({
        id: edge.id,
        type: "topology",
        source: edge.source,
        target: edge.target,
        data: { edge },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: "#64748b",
        },
      }));

      setNodes(nextNodes);
      setEdges(nextEdges);
      setIsLayingOut(false);
      window.requestAnimationFrame(() => {
        reactFlow.fitView({ padding: 0.2, duration: 200 });
      });
    }, 120);

    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [
    collapsedGroupIds,
    handleSelectService,
    handleSelectAsset,
    handleToggleGroup,
    layoutEpoch,
    reactFlow,
    setEdges,
    setNodes,
    signature,
    visibility.edges,
    visibility.memberCounts,
    visibility.nodes,
  ]);

  const handleNodeClick: NodeMouseHandler<Node<TopologyNodeRenderData>> = (_, node) => {
    const sourceNode = node.data.node.sourceNode || syntheticGraphNode(node.data.node);
    onSelectTopologyNode?.(node.data.node);
    onSelectNode?.(sourceNode);
  };

  const handleEdgeClick: EdgeMouseHandler<Edge<TopologyEdgeRenderData>> = (_, edge) => {
    if (!edge.data?.edge) {
      return;
    }
    const sourceEdge = edge.data.edge.sourceEdge || syntheticGraphEdge(edge.data.edge);
    onSelectEdge?.(sourceEdge);
  };

  return (
    <div
      className="relative w-full overflow-hidden rounded-xl border border-slate-700/80 bg-slate-950/75 shadow-[0_20px_40px_-28px_rgba(15,23,42,0.95)]"
      style={{ height: TOPOLOGY_CANVAS_HEIGHT }}
    >
      <div className="absolute left-3 top-3 z-20 flex flex-wrap items-center gap-1.5">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleFitView}
          className="h-7 border-slate-600/80 bg-slate-900/80 px-2 text-[11px] text-slate-200 hover:bg-slate-800"
        >
          <Compass className="mr-1 h-3 w-3" />
          Fit view
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleResetLayout}
          className="h-7 border-slate-600/80 bg-slate-900/80 px-2 text-[11px] text-slate-200 hover:bg-slate-800"
        >
          <ZoomIn className="mr-1 h-3 w-3" />
          Reset layout
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleCollapseAll}
          className="h-7 border-slate-600/80 bg-slate-900/80 px-2 text-[11px] text-slate-200 hover:bg-slate-800"
        >
          <Shrink className="mr-1 h-3 w-3" />
          Collapse all
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleExpandAll}
          className="h-7 border-slate-600/80 bg-slate-900/80 px-2 text-[11px] text-slate-200 hover:bg-slate-800"
        >
          Expand all
        </Button>
      </div>

      {isLayingOut && (
        <div className="absolute right-3 top-3 z-20 rounded-md border border-slate-700/80 bg-slate-900/85 px-2 py-1 text-[11px] text-slate-300">
          Rebuilding topology layout...
        </div>
      )}

      <ReactFlow<Node<TopologyNodeRenderData>, Edge<TopologyEdgeRenderData>>
        nodes={nodes}
        edges={edges}
        nodeTypes={topologyNodeTypes}
        edgeTypes={topologyEdgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        onEdgeClick={handleEdgeClick}
        proOptions={{ hideAttribution: true }}
        fitView
        minZoom={0.2}
        maxZoom={2}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
        panOnDrag
        zoomOnScroll
        className="h-full w-full"
        data-testid="territory-topology-canvas"
      >
        <MiniMap
          position="bottom-left"
          zoomable
          pannable
          className="!h-28 !w-44 !rounded-md !border !border-slate-700/80 !bg-slate-950/85"
          data-testid="territory-topology-minimap"
        />
        <Controls
          position="bottom-right"
          showInteractive={false}
          className="rounded-md border border-slate-700/80 bg-slate-900/80 text-slate-200"
        />
        <Background gap={18} size={1} color="#334155" />
      </ReactFlow>
    </div>
  );
}

export function TopologyCanvas(props: TopologyCanvasProps) {
  const hasResizeObserver =
    typeof window !== "undefined" && typeof window.ResizeObserver !== "undefined";

  if (!hasResizeObserver) {
    return <TopologyCanvasFallback {...props} />;
  }

  return (
    <ReactFlowProvider>
      <InnerTopologyCanvas {...props} />
    </ReactFlowProvider>
  );
}
