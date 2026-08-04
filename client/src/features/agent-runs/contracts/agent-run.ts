/**
 * Frontend contracts for safe subagent-run projections.
 *
 * Responsibilities:
 * - mirror backend-generated process-local subagent run payloads
 * - identify agent-run lifecycle and activity packets from task streams
 * - keep presentation state separate from streamed/hydrated run data
 */
import { isStreamPacket, type StreamEvent, type StreamPacket } from "@/types/packets";

import {
  agentAssignmentSchema,
  agentResultProjectionSchema,
  agentRunLifecycleProjectionSchema,
  localAgentRunListEnvelopeSchema,
  localAgentRunStatusProjectionSchema,
  type AgentAssignment,
  type AgentResultProjection,
  type AgentRunLifecycleProjection,
  type LocalAgentRunStatusProjection,
} from "./agent-run-schema";

export type {
  AgentAssignment,
  AgentEvidenceRef,
  AgentResultProjection,
  AgentRunLifecycleProjection,
  LocalAgentRunStatusProjection,
} from "./agent-run-schema";

export const AGENT_RUN_LIFECYCLE_CONTENT = "agent_run_lifecycle";
export const AGENT_RUN_LIFECYCLE_SUBTYPE = "agent_run_lifecycle";
export const AGENT_RUN_PRODUCER_TYPE = "subagent";

export type AgentKind = string;
export type AgentId = string;
export type AgentDisplayName = string;
export type AgentIconKey = string;

export type AgentRunStatus =
  | "queued"
  | "running"
  | "waiting_for_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type AgentRunLifecycleStatus = Exclude<AgentRunStatus, "interrupted">;

export function isAgentRunTerminalStatus(status: AgentRunStatus): boolean {
  return (
    status === "completed" ||
    status === "failed" ||
    status === "cancelled" ||
    status === "interrupted"
  );
}

export type AgentRunOutcome =
  | "completed"
  | "partial"
  | "blocked"
  | "failed"
  | "cancelled";

export type AgentCapability = string;

export interface LocalAgentRunListResponse {
  process_local: true;
  task_id: number;
  agent_runs: LocalAgentRunStatusProjection[];
}

export function readLocalAgentRuns(
  payload: unknown,
  taskId: number,
): LocalAgentRunStatusProjection[] | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const response = localAgentRunListEnvelopeSchema.safeParse(payload);
  if (!response.success || response.data.task_id !== taskId) {
    return null;
  }
  return response.data.agent_runs.flatMap(value => {
    const parsed = localAgentRunStatusProjectionSchema.safeParse(value);
    if (!parsed.success || parsed.data.task_id !== taskId) {
      return [];
    }
    return [parsed.data];
  });
}

export interface LocalAgentRunCancelResponse {
  process_local: true;
  cancelled: boolean;
  agent_run: LocalAgentRunStatusProjection;
}

export type AgentRunStreamPayload = StreamPacket | StreamEvent;

export interface AgentRunActivityIdentity {
  taskId: number;
  agentRunId: string;
  agentId: AgentId;
  agentKind: AgentKind;
  agentDisplayName: string;
  agentIconKey: AgentIconKey;
  parentTurnId: string | null;
  parentRunId: string | null;
  internalOnly: boolean;
  lifecycleVersion: number | null;
}

export function readAgentRunMetadata(
  payloadOrMetadata: unknown,
): Record<string, unknown> | null {
  const event = unwrapStreamEvent(payloadOrMetadata);
  if (event) {
    return readRecord(event.metadata);
  }
  const record = readRecord(payloadOrMetadata);
  if (!record) {
    return null;
  }
  return readRecord(record.metadata) ?? record;
}

export function hasAgentRunIdentity(metadata: unknown): boolean {
  const record = readAgentRunMetadata(metadata);
  return Boolean(
    record &&
      readString(record.agent_run_id) &&
      readString(record.agent_id),
  );
}

export function isSubagentRunMetadata(metadata: unknown): boolean {
  const record = readAgentRunMetadata(metadata);
  return Boolean(
    record &&
      readString(record.producer_type) === AGENT_RUN_PRODUCER_TYPE &&
      readString(record.agent_run_id),
  );
}

export function isAgentRunLifecyclePayload(payload: unknown): boolean {
  const event = unwrapStreamEvent(payload);
  const metadata = readAgentRunMetadata(payload);
  if (!event || !metadata || !readString(metadata.agent_run_id)) {
    return false;
  }
  return (
    event.content === AGENT_RUN_LIFECYCLE_CONTENT ||
    metadata.subtype === AGENT_RUN_LIFECYCLE_SUBTYPE
  );
}

export function isAgentRunParentControlPayload(payload: unknown): boolean {
  const metadata = readAgentRunMetadata(payload);
  return Boolean(
    metadata &&
      !isAgentRunLifecyclePayload(payload) &&
      hasAgentRunIdentity(metadata) &&
      readString(metadata.agent_kind) &&
      readString(metadata.agent_display_name) &&
      readString(metadata.status) &&
      !isSubagentRunMetadata(metadata),
  );
}

export function isAgentRunActivityPayload(payload: unknown): boolean {
  const metadata = readAgentRunMetadata(payload);
  return Boolean(
    metadata &&
      !isAgentRunLifecyclePayload(payload) &&
      hasAgentRunIdentity(metadata) &&
      isSubagentRunMetadata(metadata),
  );
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
  return normalizeAgentRunLifecycleProjection(projection, metadata);
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
    agentIconKey: resolveAgentIconKey(agentId, readAgentIconKey(metadata)),
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

export function readAgentRunStreamTimestamp(payload: unknown): number | null {
  const event = unwrapStreamEvent(payload);
  if (!event) {
    return null;
  }
  return (
    parseAgentRunTimestamp(event.timestamp) ??
    parseAgentRunTimestamp(event.metadata?.timestamp)
  );
}

export function parseAgentRunTimestamp(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
    const milliseconds = value < 100_000_000_000 ? value * 1000 : value;
    return Number.isFinite(milliseconds) ? milliseconds : null;
  }
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) ? milliseconds : null;
}

export function readAgentRunStreamPayload(payload: unknown): AgentRunStreamPayload | null {
  if (isStreamPacket(payload)) {
    return isStreamEvent(payload.obj) ? payload : null;
  }
  return isStreamEvent(payload) ? payload : null;
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
  metadata?: Record<string, unknown>,
): AgentRunLifecycleProjection | null {
  const agentRunId = readString(value.agent_run_id);
  const agentId = readString(value.agent_id);
  const conversationId = readString(value.conversation_id);
  const parentTurnId = readString(value.parent_turn_id);
  const agentKind = readString(value.agent_kind);
  const taskId = readPositiveInt(value.task_id);
  if (!agentRunId || !agentId || !agentKind || taskId === null || !conversationId || !parentTurnId) {
    return null;
  }
  const agentDisplayName = resolveAgentDisplayName(
    agentId,
    readString(value.agent_display_name),
  );
  const agentIconKey = resolveAgentIconKey(
    agentId,
    readAgentIconKey(value) ?? readAgentIconKey(metadata),
  );
  const identity = {
    agentRunId,
    agentId,
    agentKind,
    taskId,
    conversationId,
    parentTurnId,
  };
  const parsedAssignment = readAgentAssignment(value.assignment);
  const assignment =
    parsedAssignment && assignmentMatchesLifecycle(parsedAssignment, identity)
      ? parsedAssignment
      : null;
  const parsedResult = readAgentResultProjection(value.result);
  const result =
    parsedResult && resultMatchesLifecycle(parsedResult, identity)
      ? parsedResult
      : null;
  const normalized = agentRunLifecycleProjectionSchema.safeParse({
    ...value,
    agent_display_name: agentDisplayName,
    agent_icon_key: agentIconKey,
    assignment,
    result,
  });
  return normalized.success ? normalized.data : null;
}

export function readAgentAssignment(value: unknown): AgentAssignment | null {
  const parsed = agentAssignmentSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
}

export function readAgentResultProjection(value: unknown): AgentResultProjection | null {
  const parsed = agentResultProjectionSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
}

export function resolveAgentDisplayName(
  agentId: AgentId,
  reportedDisplayName?: string | null,
): AgentDisplayName {
  return reportedDisplayName?.trim() || formatAgentId(agentId);
}

export function resolveAgentIconKey(
  agentId: AgentId,
  reportedIconKey?: string | null,
): AgentIconKey {
  const normalizedIconKey = reportedIconKey?.trim();
  if (normalizedIconKey) {
    return normalizedIconKey;
  }
  return agentId.trim().toLowerCase() === "pathfinder" ? "pathfinder" : "generic";
}

function formatAgentId(agentId: string): string {
  return agentId
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function readAgentIconKey(value: Record<string, unknown> | null | undefined): string | null {
  if (!value) {
    return null;
  }
  return (
    readString(value.agent_icon_key) ??
    readString(value.agent_icon) ??
    readString(value.icon)
  );
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

interface LifecycleIdentity {
  agentRunId: string;
  agentId: string;
  agentKind: string;
  taskId: number;
  conversationId: string;
  parentTurnId: string;
}

function assignmentMatchesLifecycle(
  assignment: AgentAssignment,
  identity: LifecycleIdentity,
): boolean {
  return (
    assignment.agent_run_id === identity.agentRunId &&
    assignment.agent_id === identity.agentId &&
    assignment.agent_kind === identity.agentKind &&
    assignment.task_id === identity.taskId &&
    assignment.conversation_id === identity.conversationId &&
    assignment.parent_turn_id === identity.parentTurnId
  );
}

function resultMatchesLifecycle(
  result: AgentResultProjection,
  identity: LifecycleIdentity,
): boolean {
  return (
    result.agent_run_id === identity.agentRunId &&
    result.agent_id === identity.agentId &&
    result.agent_kind === identity.agentKind
  );
}
