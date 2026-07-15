/* Engagement-knowledge API contracts.
 *
 * Shared shapes (AssetSummary, ServiceSummary, GraphNode, GraphEdge, filters,
 * pagination, evidence-read) are canonical in knowledge.ts and re-exported here.
 * Engagement-specific types extend the knowledge base with engagement_id. */

import type {
  AssetSummary,
  ServiceSummary,
  FindingListItem as KnowledgeFindingListItem,
  AssetListItem as KnowledgeAssetListItem,
  EvidenceListItem as KnowledgeEvidenceListItem,
  GraphNode,
  GraphEdge,
  FindingsFilters,
  AssetsFilters,
  EvidenceFilters,
  EvidenceReadMode,
  EvidenceReadResponse,
  PaginatedResponse,
} from "@/types/knowledge";

export type {
  AssetSummary,
  ServiceSummary,
  GraphNode,
  GraphEdge,
  FindingsFilters,
  AssetsFilters,
  EvidenceFilters,
  EvidenceReadMode,
  EvidenceReadResponse,
  PaginatedResponse,
};

export interface EngagementListItem {
  id: number;
  user_id: number;
  name: string;
  description: string | null;
  status: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface EngagementSummary {
  engagement_id: number;
  open_findings_total: number;
  open_findings_by_severity: Record<string, number>;
  asset_counts: {
    total: number;
    vulnerable: number;
    exploited: number;
  };
  service_count: number;
  evidence_count: number;
  relationship_count: number;
  last_observed_at: string | null;
  open_statuses: string[];
}

export interface FindingListItem extends KnowledgeFindingListItem {
  engagement_id: number;
}

export interface FindingDetail extends FindingListItem {
  asset: AssetSummary | null;
  service: ServiceSummary | null;
  evidence_summary: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface AssetListItem extends KnowledgeAssetListItem {
  engagement_id: number;
}

export interface AssetDetail extends AssetListItem {
  services: ServiceSummary[];
  findings: FindingListItem[];
}

export interface EvidenceListItem extends KnowledgeEvidenceListItem {
  engagement_id: number;
}

export interface EngagementGraphSnapshot {
  engagement_id: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface WebSurfaceOriginSummary {
  origin_key: string;
  total_paths: number;
  visible_paths: number;
  hidden_noisy: number;
  calibrated_warnings: number;
  producers: string[];
  first_seen_at: string | null;
  last_seen_at: string | null;
}

export interface WebSurfaceOriginsResponse {
  service_key: string;
  items: WebSurfaceOriginSummary[];
}

export interface WebSurfacePathItem {
  canonical_url: string;
  path: string | null;
  last_status_code: number | null;
  last_response_size: number | null;
  calibrated_baseline: boolean;
  noise_score: number;
  producers: Record<
    string,
    {
      seen_count: number;
      last_seen_at: string | null;
      run_ids: string[];
    }
  >;
  first_seen_at: string | null;
  last_seen_at: string | null;
}

export interface WebSurfacePathPage {
  service_key: string | null;
  origin_key: string | null;
  items: WebSurfacePathItem[];
  total: number;
  limit: number;
  offset: number;
  hidden_noisy: number;
}
