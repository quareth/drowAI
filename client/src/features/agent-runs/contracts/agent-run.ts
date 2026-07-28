/**
 * Frontend contracts for safe subagent-run projections.
 *
 * Responsibilities:
 * - mirror backend-generated process-local subagent run payloads
 * - identify agent-run lifecycle and activity packets from task streams
 * - keep presentation state separate from streamed/hydrated run data
 */
import { isStreamPacket, type StreamEvent, type StreamPacket } from "@/types/packets";

export const AGENT_RUN_LIFECYCLE_CONTENT = "agent_run_lifecycle";
export const AGENT_RUN_LIFECYCLE_SUBTYPE = "agent_run_lifecycle";
export const AGENT_RUN_PRODUCER_TYPE = "subagent";

export type AgentKind = string;
export type AgentId = string;
export type AgentDisplayName = string;

export type AgentRunStatus =
  | "queued"
  | "running"
  | "waiting_for_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type AgentRunLifecycleStatus = Exclude<AgentRunStatus, "interrupted">;

export type AgentRunOutcome =
  | "completed"
  | "partial"
  | "blocked"
  | "failed"
  | "cancelled";

export type AgentCapability = string;

export type AgentRunDrawerView = "list" | "detail";

export interface AgentCredentialReference {
  provider: string;
  credential_id: string;
}

export interface AgentRuntimeIdentity {
  tenant_id: number;
  task_id: number;
  user_id?: number | null;
  workspace_id: string;
  workspace_path?: string | null;
  runtime_placement_mode: string;
  actor_type: string;
  actor_id: string;
  runner_id?: string | null;
  execution_site_id?: string | null;
  provider?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  feature_flags: Record<string, boolean>;
  credential_ref?: AgentCredentialReference | null;
}

export interface AgentAssignment {
  assignment_id: string;
  agent_run_id: string;
  agent_id: AgentId;
  agent_kind: AgentKind;
  task_id: number;
  tenant_id: number;
  conversation_id: string;
  parent_turn_id: string;
  parent_graph_thread_id: string;
  objective: string;
  targets: string[];
  suggested_capabilities: AgentCapability[];
  scope_summary?: string | null;
  relevant_context: Record<string, unknown>;
  runtime_identity: AgentRuntimeIdentity;
}

export interface AgentEvidenceRef {
  [key: string]: string;
}

export interface AgentResultProjection {
  agent_run_id: string;
  agent_id: AgentId;
  agent_kind: AgentKind;
  agent_display_name: AgentDisplayName;
  outcome: AgentRunOutcome;
  summary: string;
  key_findings: string[];
  evidence_refs: AgentEvidenceRef[];
  tools_used: string[];
  limitations: string[];
  recommended_next_steps: string[];
  final_checkpoint_id?: string | null;
}

export interface AgentRunLifecycleProjection {
  agent_run_id: string;
  agent_id: AgentId;
  agent_kind: AgentKind;
  agent_display_name: AgentDisplayName;
  status: AgentRunLifecycleStatus;
  lifecycle_version: number;
  task_id: number;
  conversation_id: string;
  parent_turn_id: string;
  parent_run_id?: string | null;
  assignment?: AgentAssignment | null;
  result?: AgentResultProjection | null;
  safe_error?: string | null;
}

export interface LocalAgentRunStatusProjection extends AgentRunLifecycleProjection {
  assignment: AgentAssignment;
  cancel_requested: boolean;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface LocalAgentRunListResponse {
  task_id: number;
  agent_runs: LocalAgentRunStatusProjection[];
}

export interface LocalAgentRunCancelResponse {
  cancelled: boolean;
  agent_run: LocalAgentRunStatusProjection;
}

export interface AgentRunPresentationState {
  isOpen: boolean;
  parentRunId: string | null;
  view: AgentRunDrawerView;
  selectedAgentRunId: string | null;
  activityExpanded: boolean;
}

export const CLOSED_AGENT_RUN_PRESENTATION_STATE: AgentRunPresentationState = {
  isOpen: false,
  parentRunId: null,
  view: "list",
  selectedAgentRunId: null,
  activityExpanded: false,
};

export type AgentRunStreamPayload = StreamPacket | StreamEvent;

export interface AgentRunActivityIdentity {
  taskId: number;
  agentRunId: string;
  agentId: AgentId;
  agentKind: AgentKind;
  agentDisplayName: string;
  parentTurnId: string | null;
  parentRunId: string | null;
  internalOnly: boolean;
  lifecycleVersion: number | null;
}

export function readAgentRunLifecycleProjection(
  payload: unknown,
): AgentRunLifecycleProjection | null {
  const event = unwrapStreamEvent(payload);
  if (!event) {
    return null;
  }
  const content = typeof event.content === "string" ? event.content : "";
  const metadata = event.metadata ?? {};
  const subtype = typeof metadata.subtype === "string" ? metadata.subtype : "";
  if (
    event.type !== "status" ||
    (content !== AGENT_RUN_LIFECYCLE_CONTENT && subtype !== AGENT_RUN_LIFECYCLE_SUBTYPE)
  ) {
    return null;
  }
  const projection = readRecord((event as { agent_run?: unknown }).agent_run);
  if (!projection) {
    return null;
  }
  return normalizeAgentRunLifecycleProjection(projection);
}

export function readAgentRunActivityIdentity(
  taskId: number,
  payload: unknown,
): AgentRunActivityIdentity | null {
  if (!Number.isFinite(taskId) || taskId <= 0) {
    return null;
  }
  const event = unwrapStreamEvent(payload);
  const metadata = event?.metadata ?? readPacketMetadata(payload);
  if (!metadata) {
    return null;
  }
  const producerType = readString(metadata.producer_type);
  const agentRunId = readString(metadata.agent_run_id);
  const agentId = readString(metadata.agent_id);
  const agentKind = readString(metadata.agent_kind);
  if (
    producerType !== AGENT_RUN_PRODUCER_TYPE ||
    !agentRunId ||
    !agentId ||
    !agentKind
  ) {
    return null;
  }
  return {
    taskId,
    agentRunId,
    agentId,
    agentKind,
    agentDisplayName: resolveAgentDisplayName(
      agentId,
      readString(metadata.agent_display_name),
    ),
    parentTurnId: readString(metadata.parent_turn_id),
    parentRunId: readString(metadata.parent_run_id),
    internalOnly: metadata.internal_only === true,
    lifecycleVersion: readPositiveInt(metadata.lifecycle_version),
  };
}

export function readStreamSequence(payload: unknown, fallback?: number): number | null {
  const candidates: unknown[] = [fallback];
  if (isStreamPacket(payload)) {
    candidates.push(payload.sequence, payload.obj?.metadata?.sequence);
  } else {
    const event = payload as StreamEvent | null;
    candidates.push(event?.sequence, event?.metadata?.sequence);
  }
  for (const candidate of candidates) {
    const sequence = readNonNegativeInt(candidate);
    if (sequence !== null) {
      return sequence;
    }
  }
  return null;
}

function unwrapStreamEvent(payload: unknown): StreamEvent | null {
  if (isStreamPacket(payload)) {
    return isStreamEvent(payload.obj) ? payload.obj : null;
  }
  return isStreamEvent(payload) ? payload : null;
}

function isStreamEvent(value: unknown): value is StreamEvent {
  if (!value || typeof value !== "object") {
    return false;
  }
  return typeof (value as StreamEvent).type === "string";
}

function readPacketMetadata(payload: unknown): Record<string, unknown> | null {
  if (!isStreamPacket(payload)) {
    return null;
  }
  return readRecord(payload.obj?.metadata);
}

function normalizeAgentRunLifecycleProjection(
  value: Record<string, unknown>,
): AgentRunLifecycleProjection | null {
  const agentRunId = readString(value.agent_run_id);
  const agentId = readString(value.agent_id);
  const status = readLifecycleStatus(value.status);
  const lifecycleVersion = readPositiveInt(value.lifecycle_version);
  const taskId = readPositiveInt(value.task_id);
  const conversationId = readString(value.conversation_id);
  const parentTurnId = readString(value.parent_turn_id);
  if (!agentRunId || !agentId || !status || lifecycleVersion === null || taskId === null || !conversationId || !parentTurnId) {
    return null;
  }
  const agentKind = readString(value.agent_kind);
  if (!agentKind) {
    return null;
  }
  const agentDisplayName = resolveAgentDisplayName(
    agentId,
    readString(value.agent_display_name),
  );
  return {
    agent_run_id: agentRunId,
    agent_id: agentId,
    agent_kind: agentKind,
    agent_display_name: agentDisplayName,
    status,
    lifecycle_version: lifecycleVersion,
    task_id: taskId,
    conversation_id: conversationId,
    parent_turn_id: parentTurnId,
    parent_run_id: readString(value.parent_run_id),
    assignment: readAssignment(value.assignment),
    result: readResultProjection(value.result),
    safe_error: readString(value.safe_error),
  };
}

function readAssignment(value: unknown): AgentAssignment | null {
  const record = readRecord(value);
  if (!record) {
    return null;
  }
  return record as unknown as AgentAssignment;
}

function readResultProjection(value: unknown): AgentResultProjection | null {
  const record = readRecord(value);
  if (!record) {
    return null;
  }
  if (!readString(record.agent_id) || !readString(record.agent_kind) || !readString(record.summary)) {
    return null;
  }
  return record as unknown as AgentResultProjection;
}

export function resolveAgentDisplayName(
  agentId: AgentId,
  reportedDisplayName?: string | null,
): AgentDisplayName {
  return reportedDisplayName?.trim() || formatAgentId(agentId);
}

function formatAgentId(agentId: string): string {
  return agentId
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function readRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function readString(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
}

function readPositiveInt(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  const normalized = Math.floor(value);
  return normalized > 0 ? normalized : null;
}

function readNonNegativeInt(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  const normalized = Math.floor(value);
  return normalized >= 0 ? normalized : null;
}

function readLifecycleStatus(value: unknown): AgentRunLifecycleStatus | null {
  if (typeof value !== "string") {
    return null;
  }
  switch (value) {
    case "queued":
    case "running":
    case "waiting_for_approval":
    case "completed":
    case "failed":
    case "cancelled":
      return value;
    default:
      return null;
  }
}
