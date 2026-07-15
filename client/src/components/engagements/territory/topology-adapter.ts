/* Adapter that converts engagement relationship graph payloads into topology canvas view models. */

import type {
  EngagementGraphSnapshot,
  GraphNode,
} from "@/types/engagement-knowledge";
import type {
  TopologyEdge,
  TopologyEdgeKind,
  TopologyGroup,
  TopologyFindingBadge,
  TopologyNode,
  TopologyNodeKind,
  TopologyServiceChip,
  TopologyView,
} from "@/components/engagements/territory/topology-types";
import {
  compareSeverity,
  normalizeSeverity,
} from "@/components/engagements/severity-presentation";

const NETWORK_METADATA_KEYS = ["network_id", "subnet", "cidr", "segment", "zone"];
const FINDING_EDGE_KINDS = new Set<TopologyEdgeKind>(["affects", "has_finding"]);

function toStringMetadata(
  metadata: Record<string, unknown>,
  keys: string[],
): string | null {
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }
  return null;
}

function extractIpv4Candidate(input: string | null | undefined): string | null {
  if (!input) {
    return null;
  }
  const match = input.match(/\b(\d{1,3}\.){3}\d{1,3}\b/);
  if (!match) {
    return null;
  }
  const candidate = match[0];
  const isValid = candidate.split(".").every((segment) => {
    const parsed = Number(segment);
    return Number.isInteger(parsed) && parsed >= 0 && parsed <= 255;
  });
  return isValid ? candidate : null;
}

function cidrFromIpv4(ipv4: string): string {
  const [a, b, c] = ipv4.split(".");
  return `${a}.${b}.${c}.0/24`;
}

function normalizeNetworkId(raw: string): string {
  return raw.startsWith("network:") ? raw : `network:${raw}`;
}

function inferNodeKind(node: GraphNode): TopologyNodeKind {
  const nodeType = (node.node_type || "").toLowerCase();
  const subjectKey = (node.subject_key || "").toLowerCase();

  if (
    nodeType.includes("network") ||
    nodeType.includes("subnet") ||
    nodeType.includes("segment") ||
    subjectKey.startsWith("network.")
  ) {
    return "network";
  }
  if (nodeType.includes("service") || subjectKey.startsWith("service.")) {
    return "service";
  }
  if (nodeType.includes("finding") || subjectKey.startsWith("finding.")) {
    return "finding";
  }
  return "asset";
}

function inferNetworkId(node: GraphNode): string | null {
  const metadata = node.metadata || {};
  const explicit = toStringMetadata(metadata, NETWORK_METADATA_KEYS);
  if (explicit) {
    return normalizeNetworkId(explicit);
  }

  const fromSubject = extractIpv4Candidate(node.subject_key);
  if (fromSubject) {
    return normalizeNetworkId(cidrFromIpv4(fromSubject));
  }

  const fromLabel = extractIpv4Candidate(node.label);
  if (fromLabel) {
    return normalizeNetworkId(cidrFromIpv4(fromLabel));
  }

  return null;
}

function relationshipToEdgeKind(value: string | null | undefined): TopologyEdgeKind {
  const type = (value || "").toLowerCase();
  if (type.includes("contain")) {
    return "contains";
  }
  if (type.includes("expose")) {
    return "exposes";
  }
  if (type.includes("affect")) {
    return "affects";
  }
  if (type === "has_finding") {
    return "has_finding";
  }
  if (type.includes("route") || type.includes("path")) {
    return "route";
  }
  return "related";
}

function createSyntheticNetworkNode(
  networkId: string,
  engagementId: number,
): TopologyNode {
  return {
    id: networkId,
    label: networkId.replace(/^network:/, ""),
    kind: "network",
    metadata: { inferred: true, engagement_id: engagementId },
    networkId: null,
    sourceNode: null,
    synthetic: true,
    childServices: [],
    childFindings: [],
  };
}

function addSyntheticContainsEdges(
  nodes: TopologyNode[],
  groups: Record<string, TopologyGroup>,
  edges: TopologyEdge[],
) {
  const existing = new Set<string>(
    edges.map((edge) => `${edge.source}=>${edge.target}`),
  );
  for (const group of Object.values(groups)) {
    for (const memberId of group.memberNodeIds) {
      const key = `${group.id}=>${memberId}`;
      if (existing.has(key)) {
        continue;
      }
      edges.push({
        id: `contains:${group.id}:${memberId}`,
        source: group.id,
        target: memberId,
        kind: "contains",
        label: "contains",
        metadata: { inferred: true },
        sourceEdge: null,
        synthetic: true,
      });
      existing.add(key);
    }
  }

  // Clean up groups where the network node no longer exists.
  const nodeIds = new Set(nodes.map((node) => node.id));
  for (const groupId of Object.keys(groups)) {
    if (!nodeIds.has(groupId)) {
      delete groups[groupId];
    }
  }
}

function toServiceChip(node: TopologyNode): TopologyServiceChip {
  const meta = node.metadata || {};
  const state = metadataState(meta);
  const socketMatch = (node.sourceNode?.subject_key || node.id).match(
    /^service\.socket:.+\/([^/]+)\/(\d+)$/i,
  );
  const metadataPort =
    typeof meta.port === "number" ? meta.port : (typeof state.port === "number" ? state.port : null);
  const metadataProtocol =
    typeof meta.protocol === "string"
      ? meta.protocol
      : (typeof state.protocol === "string" ? state.protocol : null);
  return {
    id: node.id,
    label: node.label,
    port: metadataPort ?? (socketMatch ? Number(socketMatch[2]) : null),
    protocol: metadataProtocol ?? (socketMatch ? socketMatch[1] : null),
    status: typeof meta.status === "string" ? meta.status : (typeof state.status === "string" ? state.status : null),
    sourceNode: node.sourceNode,
    metadata: meta,
  };
}

function metadataState(metadata: Record<string, unknown>): Record<string, unknown> {
  return typeof metadata.state === "object" && metadata.state !== null
    ? (metadata.state as Record<string, unknown>)
    : {};
}

function readFindingSeverity(metadata: Record<string, unknown>): string {
  const state = metadataState(metadata);
  return normalizeSeverity(metadata.severity ?? state.severity);
}

function readFindingStatus(metadata: Record<string, unknown>): string | null {
  const state = metadataState(metadata);
  const value = metadata.status ?? state.status;
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function toFindingBadge(node: TopologyNode): TopologyFindingBadge {
  const meta = node.metadata || {};
  return {
    id: node.id,
    label: node.label,
    severity: readFindingSeverity(meta),
    status: readFindingStatus(meta),
    sourceNode: node.sourceNode,
    metadata: meta,
  };
}

function sortFindingBadges(
  findings: TopologyFindingBadge[],
): TopologyFindingBadge[] {
  return [...findings].sort((a, b) => {
    const severityDelta = compareSeverity(a.severity, b.severity);
    if (severityDelta !== 0) {
      return severityDelta;
    }
    const labelDelta = a.label.localeCompare(b.label);
    return labelDelta !== 0 ? labelDelta : a.id.localeCompare(b.id);
  });
}

/**
 * Groups service nodes under their parent asset using `exposes` edges.
 * Absorbed services become `childServices` on the asset; orphan services stay as top-level nodes.
 * Edges targeting absorbed services are remapped to the parent asset.
 */
export function groupServicesUnderAssets(
  nodes: TopologyNode[],
  edges: TopologyEdge[],
): { nodes: TopologyNode[]; edges: TopologyEdge[] } {
  const serviceToAsset = new Map<string, string>();
  for (const edge of edges) {
    if (edge.kind !== "exposes") {
      continue;
    }
    const sourceNode = nodes.find((n) => n.id === edge.source);
    const targetNode = nodes.find((n) => n.id === edge.target);
    if (sourceNode?.kind === "asset" && targetNode?.kind === "service") {
      serviceToAsset.set(targetNode.id, sourceNode.id);
    }
  }

  if (serviceToAsset.size === 0) {
    return { nodes, edges };
  }

  const assetChildMap = new Map<string, TopologyServiceChip[]>();
  const absorbedServiceIds = new Set<string>();

  for (const [serviceId, assetId] of serviceToAsset) {
    const serviceNode = nodes.find((n) => n.id === serviceId);
    if (!serviceNode) {
      continue;
    }
    absorbedServiceIds.add(serviceId);
    const existing = assetChildMap.get(assetId) || [];
    existing.push(toServiceChip(serviceNode));
    assetChildMap.set(assetId, existing);
  }

  const resultNodes = nodes
    .filter((n) => !absorbedServiceIds.has(n.id))
    .map((n) => {
      if (n.kind === "asset" && assetChildMap.has(n.id)) {
        return { ...n, childServices: assetChildMap.get(n.id)! };
      }
      return n;
    });

  const resultEdges = edges
    .filter((e) => {
      if (e.kind === "exposes" && absorbedServiceIds.has(e.target)) {
        return false;
      }
      return true;
    })
    .map((e) => {
      if (absorbedServiceIds.has(e.target)) {
        const parentAssetId = serviceToAsset.get(e.target)!;
        return {
          ...e,
          target: parentAssetId,
          metadata: { ...e.metadata, original_target: e.target },
        };
      }
      if (absorbedServiceIds.has(e.source)) {
        const parentAssetId = serviceToAsset.get(e.source)!;
        return {
          ...e,
          source: parentAssetId,
          metadata: { ...e.metadata, original_source: e.source },
        };
      }
      return e;
    });

  return { nodes: resultNodes, edges: resultEdges };
}

/**
 * Groups finding nodes under affected assets using `has_finding` and `affects` edges.
 * Absorbed findings become asset badges; orphan findings stay as top-level annotation nodes.
 */
export function groupFindingsUnderAssets(
  nodes: TopologyNode[],
  edges: TopologyEdge[],
): { nodes: TopologyNode[]; edges: TopologyEdge[] } {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const findingToAsset = new Map<string, string>();

  for (const edge of edges) {
    if (!FINDING_EDGE_KINDS.has(edge.kind)) {
      continue;
    }
    const sourceNode = nodesById.get(edge.source);
    const targetNode = nodesById.get(edge.target);
    if (sourceNode?.kind === "asset" && targetNode?.kind === "finding") {
      findingToAsset.set(targetNode.id, sourceNode.id);
      continue;
    }
    if (sourceNode?.kind === "finding" && targetNode?.kind === "asset") {
      findingToAsset.set(sourceNode.id, targetNode.id);
    }
  }

  if (findingToAsset.size === 0) {
    return { nodes, edges };
  }

  const assetFindingMap = new Map<string, TopologyFindingBadge[]>();
  const absorbedFindingIds = new Set<string>();

  for (const [findingId, assetId] of findingToAsset) {
    const findingNode = nodesById.get(findingId);
    if (!findingNode) {
      continue;
    }
    absorbedFindingIds.add(findingId);
    const existing = assetFindingMap.get(assetId) || [];
    existing.push(toFindingBadge(findingNode));
    assetFindingMap.set(assetId, existing);
  }

  const resultNodes = nodes
    .filter((node) => !absorbedFindingIds.has(node.id))
    .map((node) => {
      if (node.kind === "asset" && assetFindingMap.has(node.id)) {
        return {
          ...node,
          childFindings: sortFindingBadges(assetFindingMap.get(node.id)!),
        };
      }
      return node;
    });

  const resultEdges = edges.filter(
    (edge) => !absorbedFindingIds.has(edge.source) && !absorbedFindingIds.has(edge.target),
  );

  return { nodes: resultNodes, edges: resultEdges };
}

export function adaptEngagementGraphToTopology(
  graph?: EngagementGraphSnapshot,
): TopologyView {
  if (!graph) {
    return {
      graph: {
        engagementId: 0,
        source: "graph",
        nodes: [],
        edges: [],
        groups: {},
      },
      sparse: true,
    };
  }

  const rawNodes: TopologyNode[] = [];

  for (const node of graph.nodes || []) {
    const kind = inferNodeKind(node);
    const networkId = kind === "network" ? null : inferNetworkId(node);
    const topologyNode: TopologyNode = {
      id: node.id,
      label: node.label || node.subject_key || node.id,
      kind,
      metadata: node.metadata || {},
      networkId,
      sourceNode: node,
      synthetic: false,
      childServices: [],
      childFindings: [],
    };
    rawNodes.push(topologyNode);
  }

  const rawEdges: TopologyEdge[] = (graph.edges || []).map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    kind: relationshipToEdgeKind(edge.relationship_type),
    label: edge.relationship_type || null,
    metadata: edge.metadata || {},
    sourceEdge: edge,
    synthetic: false,
  }));

  const servicesGrouped = groupServicesUnderAssets(rawNodes, rawEdges);
  const findingsGrouped = groupFindingsUnderAssets(
    servicesGrouped.nodes,
    servicesGrouped.edges,
  );
  const nodes = findingsGrouped.nodes;
  const edges = findingsGrouped.edges;

  const groups: Record<string, TopologyGroup> = {};
  const networkNodes = new Map<string, TopologyNode>();

  for (const node of nodes) {
    if (node.kind === "network") {
      networkNodes.set(node.id, node);
      groups[node.id] = {
        id: node.id,
        label: node.label,
        memberNodeIds: [],
      };
    }
  }

  for (const node of nodes) {
    if (node.kind === "network" || !node.networkId) {
      continue;
    }
    if (!networkNodes.has(node.networkId)) {
      const syntheticNetwork = createSyntheticNetworkNode(
        node.networkId,
        graph.engagement_id,
      );
      networkNodes.set(node.networkId, syntheticNetwork);
      nodes.push(syntheticNetwork);
      groups[node.networkId] = {
        id: node.networkId,
        label: syntheticNetwork.label,
        memberNodeIds: [],
      };
    }
    groups[node.networkId].memberNodeIds.push(node.id);
  }

  addSyntheticContainsEdges(nodes, groups, edges);

  const networkCount = nodes.filter((node) => node.kind === "network").length;
  const sparse =
    nodes.length < 4 || edges.length < 3 || networkCount === 0;

  return {
    graph: {
      engagementId: graph.engagement_id,
      source: "graph",
      nodes,
      edges,
      groups,
    },
    sparse,
  };
}
