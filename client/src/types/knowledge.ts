/* User-scoped knowledge API contracts used by query hooks. */

export interface PaginatedResponse<TItem> {
  items: TItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface KnowledgeSummary {
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

export interface EvidenceRef {
  evidence_archive_id: string;
  excerpt?: string | null;
}

export interface FindingListItem {
  id: string;
  finding_key: string | null;
  finding_type: string | null;
  subject_type: string | null;
  subject_key: string | null;
  asset_id: string | null;
  service_id: string | null;
  title: string | null;
  severity: string | null;
  status: string | null;
  assertion_level: string | null;
  confidence: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  is_exploited: boolean;
  is_open: boolean;
  is_candidate?: boolean;
  source_tool?: string | null;
  asset?: AssetSummary | null;
  service?: ServiceSummary | null;
  evidence_count: number;
  affected_asset_count?: number;
  evidence_refs: EvidenceRef[];
}

export interface AssetSummary {
  id: string;
  asset_key: string | null;
  asset_type: string | null;
  display_name: string | null;
  ip_address: string | null;
  hostname: string | null;
  status: string | null;
  last_seen_at: string | null;
}

export interface ServiceSummary {
  id: string;
  service_key: string | null;
  asset_id: string | null;
  protocol: string | null;
  port: number | null;
  service_name: string | null;
  product: string | null;
  version: string | null;
  status: string | null;
  last_seen_at: string | null;
  metadata?: Record<string, unknown>;
}

export interface FindingDetail extends FindingListItem {
  asset: AssetSummary | null;
  service: ServiceSummary | null;
  evidence_summary: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface AssetListItem {
  id: string;
  asset_key: string | null;
  asset_type: string | null;
  display_name: string | null;
  ip_address: string | null;
  hostname: string | null;
  status: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  max_confidence: string | null;
  metadata: Record<string, unknown>;
  finding_count: number;
  is_vulnerable: boolean;
  is_exploited: boolean;
  service_count: number;
}

export interface AssetDetail extends AssetListItem {
  services: ServiceSummary[];
  findings: FindingListItem[];
}

export interface EvidenceListItem {
  id: string;
  task_id: number | null;
  source_execution_id: string;
  source_artifact_id: string | null;
  storage_mode: string | null;
  content_sha256: string | null;
  byte_size: number | null;
  mime_type: string | null;
  source_tool: string | null;
  evidence_type: string | null;
  lineage: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

export interface GraphNode {
  id: string;
  subject_key: string;
  node_type: string;
  label: string;
  metadata: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  relationship_type: string | null;
  confidence: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  metadata: Record<string, unknown>;
}

export interface KnowledgeGraphSnapshot {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface FindingsFilters {
  severity?: string;
  status?: string;
  exploited?: boolean;
  asset?: string;
  source?: string;
  query?: string;
  sort?: string;
  include_candidates?: boolean;
  limit?: number;
  offset?: number;
}

export interface AssetsFilters {
  type?: string;
  vulnerable?: boolean;
  exploited?: boolean;
  query?: string;
  sort?: string;
  limit?: number;
  offset?: number;
}

export interface EvidenceFilters {
  source_tool?: string;
  type?: string;
  query?: string;
  sort?: string;
  limit?: number;
  offset?: number;
}

/* ------------------------------------------------------------------ */
/* Rich nmap metadata accessor types (additive, used by detail panels) */
/* ------------------------------------------------------------------ */

export interface NmapOsMatch {
  name: string;
  accuracy: number | null;
}

export interface NmapScriptSummary {
  script_id: string;
  summary: string;
}

export interface NmapTraceHop {
  ttl: number;
  ip: string;
  host: string | null;
  rtt_ms: number | null;
}

export interface NmapServiceProfile {
  http_title?: string;
  server_header?: string;
  script_summaries?: NmapScriptSummary[];
}

/** Typed accessor for rich host state within asset metadata.state */
export interface RichHostState {
  host_status?: string;
  hostnames?: string[];
  os_top_guess?: string;
  os_matches?: NmapOsMatch[];
  host_script_summaries?: NmapScriptSummary[];
  trace_summary?: {
    hop_count: number;
    hops: NmapTraceHop[];
  };
}

/** Typed accessor for rich service state within service metadata.state */
export interface RichServiceState {
  service_name?: string;
  product?: string;
  version?: string;
  version_raw?: string;
  version_relation?: string;
  http_title?: string;
  server_header?: string;
  script_summaries?: NmapScriptSummary[];
}

/** Inputs for the shared service fingerprint formatter. */
export interface ServiceFingerprintInput {
  product?: string | null;
  version?: string | null;
  versionRaw?: string | null;
  versionRelation?: string | null;
}

/**
 * Format a normalized service fingerprint string from product/version fields.
 * Shared across asset detail, finding detail, and territory inspector surfaces.
 */
export function formatServiceFingerprint(input: ServiceFingerprintInput): string | null {
  const product = (input.product || "").trim();
  const version = (input.versionRaw || input.version || "").trim();
  if (!product && !version) return null;
  if (product && version) {
    return `${product} ${input.versionRelation === "gte" && !input.versionRaw ? `${version}+` : version}`;
  }
  return product || version;
}

/* ------------------------------------------------------------------ */
/* Rich nuclei finding metadata accessor types                         */
/* ------------------------------------------------------------------ */

export interface NucleiClassification {
  cve_ids?: string[];
  cwe_ids?: string[];
}

/** Typed accessor for rich nuclei finding state within finding metadata.state */
export interface RichNucleiFindingState {
  finding_presence?: string;
  severity?: string;
  detector_id?: string;
  matcher_id?: string;
  title?: string;
  description_summary?: string;
  matched_at?: string;
}

/** Typed accessor for rich nuclei finding details within finding metadata.rich_details */
export interface RichNucleiFindingDetails {
  classification?: NucleiClassification;
  tags?: string[];
  references?: string[];
  extracted_results?: string[];
}

/** Helper to extract typed state from metadata Record. */
export function getMetadataState<T>(
  metadata: Record<string, unknown> | undefined
): T | undefined {
  if (!metadata) return undefined;
  const state = metadata.state;
  if (state && typeof state === "object") return state as T;
  return undefined;
}

/** Helper to extract typed rich_details from metadata Record. */
export function getMetadataRichDetails<T>(
  metadata: Record<string, unknown> | undefined
): T | undefined {
  if (!metadata) return undefined;
  const details = metadata.rich_details;
  if (details && typeof details === "object") return details as T;
  return undefined;
}

export type EvidenceReadMode = "auto" | "head" | "tail" | "match" | "full";

export interface EvidenceReadResponse {
  status: "ready" | "not_found" | "not_available";
  evidence_archive_id: string;
  storage_mode: string;
  content: string | null;
  mode_used: EvidenceReadMode;
  truncated: boolean;
  source: "inline_excerpt" | "object_ref" | "archived_file" | "none";
}
