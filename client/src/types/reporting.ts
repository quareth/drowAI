/* Frontend wire contracts for the engagement reporting API. */

export type ISODateString = string;
export type UUIDString = string;

export type ReportType = "pentest" | "vulnerability_assessment";
export type ReportingInputState =
  | "not_prepared"
  | "preparing"
  | "ready"
  | "failed"
  | "stale";
export type ReportJobStatus =
  | "queued"
  | "generating"
  | "ready"
  | "failed"
  | "cancelled";
export type ReportGenerationPhase = "sections" | "finalizing";
export type MemoStatus = "preparing" | "ready" | "failed";
export type MemoMode = "supported" | "limited";
export type ReportStatus = "generating" | "ready" | "failed";
export type ReportingReasonCode =
  | "task_not_stopped"
  | "runtime_retirement_not_confirmed"
  | "no_useful_runtime_execution"
  | "no_reportable_or_limited_source_material";
export type MemoTextSource = "transcript" | "evidence" | "knowledge";
export type MemoConfidence = "low" | "medium" | "high";
export type MemoSeverityHint =
  | "informational"
  | "low"
  | "medium"
  | "high"
  | "critical";
export type ReportSectionStatus = "ready" | "needs_review" | "failed";
export type ReportSectionType =
  | "narrative"
  | "summary"
  | "findings"
  | "recommendations"
  | "limitations"
  | "appendix";
export type ReportSectionBlockType =
  | "finding"
  | "evidence_note"
  | "asset_note"
  | "appendix_note";

export interface SourceWatermarkSnapshot {
  last_chat_message_id: number | null;
  last_turn_sequence: number | null;
  latest_tool_execution_id: string | null;
  latest_evidence_created_at: ISODateString | null;
  latest_knowledge_observed_at: ISODateString | null;
}

export interface ReportingSourceCounts {
  evidence: number;
  canonical_findings: number;
  candidate_findings: number;
}

export interface TaskClosureMemoIncludeInReportRecommendation {
  include: boolean;
  reason: string;
}

export interface TaskClosureMemoActionItem {
  text: string;
  source: MemoTextSource;
}

export interface TaskClosureMemoReportableObservationItem {
  text: string;
  confidence: MemoConfidence;
  evidence_refs: string[];
  knowledge_refs: string[];
}

export interface TaskClosureMemoPossibleFindingItem {
  title: string;
  severity_hint: MemoSeverityHint | null;
  confidence: MemoConfidence;
  description: string | null;
  evidence_refs: string[];
  knowledge_refs: string[];
}

export interface TaskClosureMemoLimitationItem {
  text: string;
}

export interface TaskClosureMemoUnsupportedNoteItem {
  text: string;
}

export interface TaskClosureMemoBody {
  task_name: string;
  summary: string;
  include_in_report_recommendation: TaskClosureMemoIncludeInReportRecommendation;
  actions_performed: TaskClosureMemoActionItem[];
  reportable_observations: TaskClosureMemoReportableObservationItem[];
  possible_findings: TaskClosureMemoPossibleFindingItem[];
  limitations: TaskClosureMemoLimitationItem[];
  unsupported_notes: TaskClosureMemoUnsupportedNoteItem[];
  evidence_refs: string[];
  knowledge_refs: string[];
}

export interface TaskClosureMemoSummary {
  id: UUIDString;
  version: number;
  status: MemoStatus;
  memo_mode: MemoMode;
  is_current: boolean;
  source_watermark: SourceWatermarkSnapshot;
  error_message: string | null;
  created_at: ISODateString;
  updated_at: ISODateString;
  generated_at: ISODateString | null;
}

export interface TaskClosureMemoAttemptSummary {
  id: UUIDString;
  schema_version: string;
  engagement_id: number;
  task_id: number;
  version: number;
  status: MemoStatus;
  memo_mode: MemoMode;
  is_current: boolean;
  source_watermark: Record<string, unknown>;
  error_message: string | null;
  created_at: ISODateString;
  updated_at: ISODateString;
  generated_at: ISODateString | null;
}

export interface TaskClosureMemoReadResponse extends TaskClosureMemoAttemptSummary {
  body: TaskClosureMemoBody | null;
}

export interface TaskClosureMemoPrepareRequest {
  regenerate: boolean;
}

export interface TaskClosureMemoPrepareResponse {
  task_id: number;
  memo: TaskClosureMemoReadResponse;
}

export interface ReportingInputTaskRow {
  task_id: number;
  task_name: string;
  task_status: string;
  runtime_retired: boolean;
  is_reportable: boolean;
  is_preparable: boolean;
  memo_mode: MemoMode | null;
  not_preparable_reason: ReportingReasonCode | null;
  input_state: ReportingInputState;
  current_memo: TaskClosureMemoSummary | null;
  latest_memo_attempt: TaskClosureMemoSummary | null;
  source_watermark: SourceWatermarkSnapshot;
  counts: ReportingSourceCounts;
  candidate_findings_require_explicit_inclusion: boolean;
}

export interface EngagementReportingInputsResponse {
  engagement_id: number;
  tasks: ReportingInputTaskRow[];
}

export interface EngagementReportGenerationRequest {
  report_type: ReportType;
  selected_task_memo_ids: UUIDString[];
  include_candidate_findings: boolean;
  force_regenerate: boolean;
}

export interface EngagementReportGenerationResponse {
  job_id: UUIDString | null;
  report_id: UUIDString | null;
  status: ReportJobStatus | ReportStatus;
}

export interface EngagementReportDeleteResponse {
  report_id: UUIDString;
  engagement_id: number;
  report_type: ReportType;
  deleted_current: boolean;
  current_report_id: UUIDString | null;
  undo_until: ISODateString;
}

export interface EngagementReportUndoDeleteResponse {
  report_id: UUIDString;
  engagement_id: number;
  report_type: ReportType;
  restored_current: boolean;
  current_report_id: UUIDString | null;
}

export interface EngagementReportSectionSourceRefs {
  task_memo_ids: string[];
  knowledge_refs: string[];
  evidence_refs: string[];
}

export interface EngagementReportSectionBlock {
  block_id: string;
  block_type: ReportSectionBlockType;
  title: string;
  severity: MemoSeverityHint | null;
  confidence: MemoConfidence | null;
  affected_assets: string[];
  content_markdown: string;
  impact_markdown: string;
  remediation_markdown: string;
  source_refs: EngagementReportSectionSourceRefs;
}

export interface EngagementReportSection {
  schema_version: string;
  section_id: string;
  section_type: ReportSectionType;
  title: string;
  status: ReportSectionStatus;
  content_markdown: string;
  blocks: EngagementReportSectionBlock[];
  source_refs: EngagementReportSectionSourceRefs;
  unsupported_notes: string[];
  generation_notes: string[];
}

export interface EngagementReportSourceKnowledgeRef {
  ref: string;
  task_id: number;
  record_type: string;
  authoritative: boolean;
}

export interface EngagementReportSourceEvidenceRef {
  ref: string;
  task_id: number;
  evidence_type: string;
  source_tool: string;
}

export interface EngagementReportSummary {
  id: UUIDString;
  engagement_id: number;
  engagement_name_snapshot: string | null;
  engagement_status_snapshot: string | null;
  report_type: ReportType;
  version: number;
  status: ReportStatus;
  is_current: boolean;
  title: string;
  sections: Record<string, unknown>[];
  markdown_snapshot: string | null;
  source_task_memo_ids: string[];
  source_knowledge_refs: EngagementReportSourceKnowledgeRef[];
  source_evidence_refs: EngagementReportSourceEvidenceRef[];
  generation_metadata: Record<string, unknown> | null;
  error_message: string | null;
  created_at: ISODateString;
  updated_at: ISODateString;
  generated_at: ISODateString | null;
}

export interface EngagementReportReadResponse
  extends Omit<EngagementReportSummary, "sections"> {
  schema_version: string;
  sections: EngagementReportSection[];
}

export interface EngagementReportHistoryItem {
  report_id: UUIDString;
  engagement_id: number;
  engagement_name_snapshot: string | null;
  engagement_status_snapshot: string | null;
  report_type: ReportType;
  version: number;
  status: ReportStatus;
  is_current: boolean;
  title: string;
  source_task_memo_ids: string[];
  source_knowledge_refs: EngagementReportSourceKnowledgeRef[];
  source_evidence_refs: EngagementReportSourceEvidenceRef[];
  generation_metadata: Record<string, unknown> | null;
  error_message: string | null;
  created_at: ISODateString;
  updated_at: ISODateString;
  generated_at: ISODateString | null;
}

export interface ReportLibraryItem {
  report_id: UUIDString;
  engagement_id: number;
  engagement_name_snapshot: string | null;
  engagement_status_snapshot: string | null;
  report_type: ReportType;
  version: number;
  status: ReportStatus;
  is_current: boolean;
  title: string;
  source_task_count: number;
  source_knowledge_count: number;
  source_evidence_count: number;
  created_at: ISODateString;
  updated_at: ISODateString;
  generated_at: ISODateString | null;
}

export interface ReportLibraryResponse {
  reports: ReportLibraryItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CurrentEngagementReportResponse {
  engagement_id: number;
  report_type: ReportType;
  report: EngagementReportReadResponse | null;
}

export interface EngagementReportHistoryResponse {
  engagement_id: number;
  report_type: ReportType;
  reports: EngagementReportHistoryItem[];
}

export interface EngagementReportJobValidationIssue {
  code: string;
  path: string;
}

export interface EngagementReportJobFailureDetails {
  failed_section_id: string | null;
  failed_section_order: number | null;
  failed_section_type: string | null;
  validation_issues: EngagementReportJobValidationIssue[];
}

export interface EngagementReportJobStatusResponse {
  id: UUIDString;
  engagement_id: number;
  report_id: UUIDString | null;
  report_type: ReportType;
  status: ReportJobStatus;
  generation_phase: ReportGenerationPhase;
  selected_task_memo_ids: string[];
  include_candidate_findings: boolean;
  source_watermark: Record<string, unknown>;
  current_section_id: string | null;
  completed_sections: string[];
  total_sections: number;
  next_attempt_at: ISODateString | null;
  attempt_count: number;
  max_attempts: number;
  last_error_code: string | null;
  error_message: string | null;
  last_error_at: ISODateString | null;
  failure_details?: EngagementReportJobFailureDetails | null;
  created_at: ISODateString;
  updated_at: ISODateString;
  started_at: ISODateString | null;
  finished_at: ISODateString | null;
}

export interface EngagementReportActiveJobResponse {
  job: EngagementReportJobStatusResponse | null;
}
