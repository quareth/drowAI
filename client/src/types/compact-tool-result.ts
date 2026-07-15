/**
 * Shared compact tool-result contracts used by chat and streaming types.
 *
 * These interfaces mirror the normalized backend compact tool payload shape
 * so frontend consumers reuse one canonical definition.
 */

export interface CompactToolArtifactReference {
  path: string;
  artifact_id?: string;
  execution_id?: string;
}

export interface CompactToolCompressionMetadata {
  source?: "llm" | "deterministic" | "hybrid" | string;
  model?: string | null;
  token_usage?: number | null;
  fallback_reason?: string | null;
}

export interface CompactToolStructuredSignal {
  type:
    | 'service'
    | 'header'
    | 'redirect'
    | 'path'
    | 'ui_link'
    | 'form'
    | 'endpoint'
    | 'error_context'
    | 'kv_pair';
  [key: string]: unknown;
}

export interface CompactToolResult {
  schema_version: string;
  tool: string;
  status: string;
  success: boolean;
  exit_code?: number | null;
  summary: string;
  key_findings: string[];
  errors: string[];
  report_recommendations: string[];
  structured_signals: CompactToolStructuredSignal[];
  decision_evidence: string[];
  lossiness_risk: 'low' | 'medium' | 'high';
  artifact_refs?: CompactToolArtifactReference[];
  compression?: CompactToolCompressionMetadata;
}
