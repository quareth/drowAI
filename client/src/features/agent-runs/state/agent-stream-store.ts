/**
 * Task-scoped subagent-run stream store.
 *
 * Responsibilities:
 * - merge process-local lifecycle projections by monotonic lifecycle version
 * - retain bounded, sequence-ordered subagent activity per task/run
 * - keep drawer presentation state separate from stream and replay hydration
 */
import { useSyncExternalStore } from "react";

import { terminalizeAgentRunStreams } from "@/state/chat-stream-store";
import { isStreamPacket, type StreamEvent, type StreamPacket } from "@/types/packets";

import {
  CLOSED_AGENT_RUN_PRESENTATION_STATE,
  readAgentRunActivityIdentity,
  readAgentRunLifecycleProjection,
  readStreamSequence,
  resolveAgentDisplayName,
  resolveAgentIconKey,
  type AgentAssignment,
  type AgentIconKey,
  type AgentId,
  type AgentKind,
  type AgentResultProjection,
  type AgentRunActivityIdentity,
  type AgentRunLifecycleProjection,
  type AgentRunPresentationState,
  type AgentRunStatus,
  type AgentRunStreamPayload,
  type LocalAgentRunStatusProjection,
} from "../contracts/agent-run";

export const MAX_AGENT_RUN_ACTIVITY_EVENTS = 5000;

export interface AgentRunActivityEntry {
  taskId: number;
  agentRunId: string;
  sequence: number | null;
  payload: AgentRunStreamPayload;
  receivedAt: number;
}

export interface AgentRunRecord {
  taskId: number;
  agentRunId: string;
  agentId: AgentId;
  agentKind: AgentKind;
  agentDisplayName: string;
  agentIconKey: AgentIconKey;
  status: AgentRunStatus;
  lifecycleVersion: number;
  conversationId: string;
  parentTurnId: string;
  parentRunId: string | null;
  assignment: AgentAssignment | null;
  result: AgentResultProjection | null;
  safeError: string | null;
  firstSequence: number | null;
  lastSequence: number | null;
  createdAt: number;
  completedAt: number | null;
  updatedAt: number;
  activity: AgentRunActivityEntry[];
}

export interface AgentRunsSnapshot {
  runs: AgentRunRecord[];
  runsById: Record<string, AgentRunRecord>;
  presentation: AgentRunPresentationState;
  version: number;
}

interface AgentRunsTaskState {
  runs: Map<string, AgentRunRecord>;
  presentation: AgentRunPresentationState;
  version: number;
}

interface MutationResult {
  changed: boolean;
}

const defaultSnapshot: AgentRunsSnapshot = {
  runs: [],
  runsById: {},
  presentation: CLOSED_AGENT_RUN_PRESENTATION_STATE,
  version: 0,
};

const taskStates = new Map<number, AgentRunsTaskState>();
const snapshotCache = new Map<number, AgentRunsSnapshot>();
const listeners = new Set<() => void>();

function ensureState(taskId: number): AgentRunsTaskState {
  let state = taskStates.get(taskId);
  if (!state) {
    state = {
      runs: new Map<string, AgentRunRecord>(),
      presentation: { ...CLOSED_AGENT_RUN_PRESENTATION_STATE },
      version: 0,
    };
    taskStates.set(taskId, state);
    snapshotCache.set(taskId, buildSnapshot(state));
  }
  return state;
}

function buildSnapshot(state: AgentRunsTaskState): AgentRunsSnapshot {
  const runs = Array.from(state.runs.values()).sort(compareRuns);
  const runsById: Record<string, AgentRunRecord> = {};
  for (const run of runs) {
    runsById[run.agentRunId] = run;
  }
  return {
    runs,
    runsById,
    presentation: state.presentation,
    version: state.version,
  };
}

function compareRuns(a: AgentRunRecord, b: AgentRunRecord): number {
  const aSequence = a.firstSequence ?? Number.MAX_SAFE_INTEGER;
  const bSequence = b.firstSequence ?? Number.MAX_SAFE_INTEGER;
  if (aSequence !== bSequence) {
    return aSequence - bSequence;
  }
  return a.agentRunId.localeCompare(b.agentRunId);
}

function cloneState(state: AgentRunsTaskState): AgentRunsTaskState {
  return {
    runs: new Map(
      Array.from(state.runs.entries()).map(([runId, run]) => [
        runId,
        { ...run, activity: [...run.activity] },
      ]),
    ),
    presentation: { ...state.presentation },
    version: state.version,
  };
}

function emit(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch {
      // Listener failures must not break store fan-out.
    }
  }
}

function mutateTaskState(
  taskId: number,
  mutator: (draft: AgentRunsTaskState) => MutationResult,
): void {
  if (!Number.isFinite(taskId) || taskId <= 0) {
    return;
  }
  const current = ensureState(taskId);
  const draft = cloneState(current);
  const result = mutator(draft);
  if (!result.changed) {
    return;
  }
  draft.version = current.version + 1;
  taskStates.set(taskId, draft);
  snapshotCache.set(taskId, buildSnapshot(draft));
  emit();
}

export function applyAgentRunLifecycleUpdate(
  taskId: number,
  projection: AgentRunLifecycleProjection,
  sequence?: number | null,
): void {
  if (projection.task_id !== taskId) {
    return;
  }
  const streamSequence = sequence ?? null;
  mutateTaskState(taskId, draft => {
    const existing = draft.runs.get(projection.agent_run_id);
    if (existing && projection.lifecycle_version < existing.lifecycleVersion) {
      return { changed: false };
    }
    const now = Date.now();
    const terminal = isTerminalStatus(projection.status);
    const next: AgentRunRecord = {
      taskId,
      agentRunId: projection.agent_run_id,
      agentId: projection.agent_id,
      agentKind: projection.agent_kind,
      agentDisplayName: resolveAgentDisplayName(
        projection.agent_id,
        projection.agent_display_name,
      ),
      agentIconKey: resolveAgentIconKey(
        projection.agent_id,
        projection.agent_icon_key,
      ),
      status: projection.status,
      lifecycleVersion: projection.lifecycle_version,
      conversationId: projection.conversation_id,
      parentTurnId: projection.parent_turn_id,
      parentRunId: projection.parent_run_id ?? existing?.parentRunId ?? null,
      assignment: projection.assignment ?? existing?.assignment ?? null,
      result: projection.result ?? existing?.result ?? null,
      safeError: projection.safe_error ?? existing?.safeError ?? null,
      firstSequence: minKnownSequence(existing?.firstSequence ?? null, streamSequence),
      lastSequence: maxKnownSequence(existing?.lastSequence ?? null, streamSequence),
      createdAt: existing?.createdAt ?? now,
      completedAt: terminal ? existing?.completedAt ?? now : existing?.completedAt ?? null,
      updatedAt: now,
      activity: existing?.activity ?? [],
    };
    if (existing && sameRunRecord(existing, next)) {
      return { changed: false };
    }
    draft.runs.set(next.agentRunId, next);
    return { changed: true };
  });
  if (isTerminalStatus(projection.status)) {
    terminalizeAgentRunStreams(taskId, projection.agent_run_id, streamSequence);
  }
}

export function applyAgentRunLifecyclePayload(
  taskId: number,
  payload: unknown,
  sequenceHint?: number,
): boolean {
  const projection = readAgentRunLifecycleProjection(payload);
  if (!projection) {
    return false;
  }
  applyAgentRunLifecycleUpdate(taskId, projection, readStreamSequence(payload, sequenceHint));
  return projection.task_id === taskId;
}

export function applyAgentRunActivityPayload(
  taskId: number,
  payload: StreamPacket | StreamEvent,
  sequenceHint?: number,
): boolean {
  const identity = readAgentRunActivityIdentity(taskId, payload);
  if (!identity || identity.internalOnly) {
    return false;
  }
  const projection = readAgentRunLifecycleProjection(payload);
  if (projection) {
    applyAgentRunLifecycleUpdate(taskId, projection, readStreamSequence(payload, sequenceHint));
    return projection.task_id === taskId;
  }
  const sequence = readStreamSequence(payload, sequenceHint);
  mutateTaskState(taskId, draft => {
    const existing = draft.runs.get(identity.agentRunId);
    const nextRun = existing ?? emptyRunFromActivity(identity);
    const activity = upsertActivity(nextRun.activity, {
      taskId,
      agentRunId: identity.agentRunId,
      sequence,
      payload,
      receivedAt: Date.now(),
    });
    if (activity === nextRun.activity) {
      return { changed: false };
    }
    draft.runs.set(identity.agentRunId, {
      ...nextRun,
      agentId: identity.agentId,
      agentDisplayName: resolveAgentDisplayName(
        identity.agentId,
        identity.agentDisplayName,
      ),
      agentIconKey: resolveAgentIconKey(identity.agentId, identity.agentIconKey),
      parentRunId: identity.parentRunId ?? nextRun.parentRunId,
      firstSequence: minKnownSequence(nextRun.firstSequence, sequence),
      lastSequence: maxKnownSequence(nextRun.lastSequence, sequence),
      updatedAt: Date.now(),
      activity,
    });
    return { changed: true };
  });
  return true;
}

export function reconcileAgentRunsWithLocalStatus(
  taskId: number,
  localRuns: LocalAgentRunStatusProjection[],
): void {
  const localRunsById = new Map<string, LocalAgentRunStatusProjection>();
  const interruptedRunIds: string[] = [];
  for (const run of localRuns) {
    if (run.task_id !== taskId) {
      continue;
    }
    localRunsById.set(run.agent_run_id, run);
    applyAgentRunLifecycleUpdate(taskId, run);
  }

  mutateTaskState(taskId, draft => {
    let changed = false;
    const now = Date.now();
    for (const [agentRunId, run] of draft.runs.entries()) {
      if (localRunsById.has(agentRunId) || isTerminalStatus(run.status)) {
        continue;
      }
      draft.runs.set(agentRunId, {
        ...run,
        status: "interrupted",
        safeError:
          run.safeError ??
          "This subagent run was replayed, but the current backend process no longer owns it.",
        completedAt: run.completedAt ?? now,
        updatedAt: now,
      });
      interruptedRunIds.push(agentRunId);
      changed = true;
    }
    return { changed };
  });
  for (const agentRunId of interruptedRunIds) {
    terminalizeAgentRunStreams(taskId, agentRunId);
  }
}

export function openAgentRunList(taskId: number, parentRunId: string): void {
  const normalizedParentRunId = parentRunId.trim();
  if (!normalizedParentRunId) {
    return;
  }
  mutateTaskState(taskId, draft => {
    const next: AgentRunPresentationState = {
      isOpen: true,
      parentRunId: normalizedParentRunId,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    };
    if (samePresentationState(draft.presentation, next)) {
      return { changed: false };
    }
    draft.presentation = next;
    return { changed: true };
  });
}

export function openAgentRunDetail(
  taskId: number,
  parentRunId: string,
  agentRunId: string,
): void {
  const normalizedParentRunId = parentRunId.trim();
  const normalizedAgentRunId = agentRunId.trim();
  if (!normalizedParentRunId || !normalizedAgentRunId) {
    return;
  }
  mutateTaskState(taskId, draft => {
    const next: AgentRunPresentationState = {
      isOpen: true,
      parentRunId: normalizedParentRunId,
      view: "detail",
      selectedAgentRunId: normalizedAgentRunId,
      activityExpanded: false,
    };
    if (samePresentationState(draft.presentation, next)) {
      return { changed: false };
    }
    draft.presentation = next;
    return { changed: true };
  });
}

export function getAgentRunParentGroupingKey(run: AgentRunRecord): string {
  return run.parentRunId ?? run.parentTurnId;
}

export function setAgentRunActivityExpanded(taskId: number, expanded: boolean): void {
  mutateTaskState(taskId, draft => {
    if (draft.presentation.activityExpanded === expanded) {
      return { changed: false };
    }
    draft.presentation = {
      ...draft.presentation,
      activityExpanded: expanded,
    };
    return { changed: true };
  });
}

export function returnAgentRunDrawerToList(taskId: number): void {
  mutateTaskState(taskId, draft => {
    if (!draft.presentation.isOpen || draft.presentation.view === "list") {
      return { changed: false };
    }
    draft.presentation = {
      ...draft.presentation,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    };
    return { changed: true };
  });
}

export function closeAgentRunDrawer(taskId: number): void {
  mutateTaskState(taskId, draft => {
    if (samePresentationState(draft.presentation, CLOSED_AGENT_RUN_PRESENTATION_STATE)) {
      return { changed: false };
    }
    draft.presentation = { ...CLOSED_AGENT_RUN_PRESENTATION_STATE };
    return { changed: true };
  });
}

export function getAgentRunSnapshot(taskId: number | null | undefined): AgentRunsSnapshot {
  if (typeof taskId !== "number" || !Number.isFinite(taskId) || taskId <= 0) {
    return defaultSnapshot;
  }
  const cached = snapshotCache.get(taskId);
  if (cached) {
    return cached;
  }
  const state = taskStates.get(taskId);
  if (!state) {
    return defaultSnapshot;
  }
  const snapshot = buildSnapshot(state);
  snapshotCache.set(taskId, snapshot);
  return snapshot;
}

export function getAgentRun(
  taskId: number | null | undefined,
  agentRunId: string | null | undefined,
): AgentRunRecord | null {
  if (typeof taskId !== "number" || !Number.isFinite(taskId) || taskId <= 0) {
    return null;
  }
  const normalizedAgentRunId = typeof agentRunId === "string" ? agentRunId.trim() : "";
  if (!normalizedAgentRunId) {
    return null;
  }
  return taskStates.get(taskId)?.runs.get(normalizedAgentRunId) ?? null;
}

export function clearAgentRunStateForTask(taskId: number): void {
  if (!taskStates.has(taskId)) {
    return;
  }
  taskStates.delete(taskId);
  snapshotCache.delete(taskId);
  emit();
}

export function resetAgentRunStoreForTests(): void {
  taskStates.clear();
  snapshotCache.clear();
  emit();
}

export function useAgentRunStoreSnapshot(taskId: number | null | undefined): AgentRunsSnapshot {
  return useSyncExternalStore(
    subscribe,
    () => getAgentRunSnapshot(taskId),
    () => defaultSnapshot,
  );
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function emptyRunFromActivity(identity: AgentRunActivityIdentity): AgentRunRecord {
  return {
    taskId: identity.taskId,
    agentRunId: identity.agentRunId,
    agentId: identity.agentId,
    agentKind: identity.agentKind,
    agentDisplayName: resolveAgentDisplayName(
      identity.agentId,
      identity.agentDisplayName,
    ),
    agentIconKey: resolveAgentIconKey(identity.agentId, identity.agentIconKey),
    status: "running",
    lifecycleVersion: identity.lifecycleVersion ?? 0,
    conversationId: "",
    parentTurnId: identity.parentTurnId ?? "",
    parentRunId: identity.parentRunId,
    assignment: null,
    result: null,
    safeError: null,
    firstSequence: null,
    lastSequence: null,
    createdAt: Date.now(),
    completedAt: null,
    updatedAt: Date.now(),
    activity: [],
  };
}

function upsertActivity(
  current: AgentRunActivityEntry[],
  entry: AgentRunActivityEntry,
): AgentRunActivityEntry[] {
  const key = activityKey(entry);
  const next = [...current];
  const existingIndex = next.findIndex(item => activityKey(item) === key);
  if (existingIndex >= 0) {
    const existing = next[existingIndex];
    if (existing.payload === entry.payload && existing.sequence === entry.sequence) {
      return current;
    }
    next[existingIndex] = entry;
  } else {
    next.push(entry);
  }
  next.sort(compareActivity);
  if (next.length > MAX_AGENT_RUN_ACTIVITY_EVENTS) {
    return next.slice(next.length - MAX_AGENT_RUN_ACTIVITY_EVENTS);
  }
  return next;
}

function activityKey(entry: AgentRunActivityEntry): string {
  const event = isStreamPacket(entry.payload) ? entry.payload.obj : entry.payload;
  const metadata = event.metadata ?? {};
  const type = typeof event.type === "string" ? event.type : "unknown";
  const eventId = typeof metadata.id === "string" ? metadata.id : "";
  const ind = typeof metadata.ind === "number" ? metadata.ind : "";
  const toolCallId = typeof metadata.tool_call_id === "string" ? metadata.tool_call_id : "";
  return `${entry.sequence ?? "unsequenced"}::${type}::${eventId}::${ind}::${toolCallId}`;
}

function compareActivity(a: AgentRunActivityEntry, b: AgentRunActivityEntry): number {
  const aSequence = a.sequence ?? Number.MAX_SAFE_INTEGER;
  const bSequence = b.sequence ?? Number.MAX_SAFE_INTEGER;
  if (aSequence !== bSequence) {
    return aSequence - bSequence;
  }
  return activityKey(a).localeCompare(activityKey(b));
}

function minKnownSequence(current: number | null, incoming: number | null): number | null {
  if (incoming === null) {
    return current;
  }
  if (current === null) {
    return incoming;
  }
  return Math.min(current, incoming);
}

function maxKnownSequence(current: number | null, incoming: number | null): number | null {
  if (incoming === null) {
    return current;
  }
  if (current === null) {
    return incoming;
  }
  return Math.max(current, incoming);
}

function isTerminalStatus(status: AgentRunStatus): boolean {
  return (
    status === "completed" ||
    status === "failed" ||
    status === "cancelled" ||
    status === "interrupted"
  );
}

function sameRunRecord(a: AgentRunRecord, b: AgentRunRecord): boolean {
  return (
    a.status === b.status &&
    a.agentId === b.agentId &&
    a.agentKind === b.agentKind &&
    a.agentDisplayName === b.agentDisplayName &&
    a.agentIconKey === b.agentIconKey &&
    a.lifecycleVersion === b.lifecycleVersion &&
    a.parentRunId === b.parentRunId &&
    a.assignment === b.assignment &&
    a.result === b.result &&
    a.safeError === b.safeError &&
    a.firstSequence === b.firstSequence &&
    a.lastSequence === b.lastSequence &&
    a.activity === b.activity
  );
}

function samePresentationState(
  a: AgentRunPresentationState,
  b: AgentRunPresentationState,
): boolean {
  return (
    a.isOpen === b.isOpen &&
    a.parentRunId === b.parentRunId &&
    a.view === b.view &&
    a.selectedAgentRunId === b.selectedAgentRunId &&
    a.activityExpanded === b.activityExpanded
  );
}
