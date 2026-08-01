/**
 * Task-scoped subagent-run stream store.
 *
 * Responsibilities:
 * - merge process-local lifecycle projections by monotonic lifecycle version
 * - retain bounded, sequence-ordered subagent activity per task/run
 * - expose stable task-scoped data snapshots for stream and replay consumers
 */
import { useSyncExternalStore } from "react";

import { terminalizeAgentRunStreams } from "@/state/chat-stream-store";
import { isStreamPacket } from "@/types/packets";

import {
  isAgentRunTerminalStatus,
  readAgentRunActivityIdentity,
  readAgentRunLifecycleProjection,
  readAgentRunStreamPayload,
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
  type AgentRunStatus,
  type AgentRunStreamPayload,
  type LocalAgentRunStatusProjection,
} from "../contracts/agent-run";

export const MAX_AGENT_RUN_ACTIVITY_EVENTS = 5000;
export const MAX_AGENT_RUN_TASK_STATES = 20;
export const MAX_AGENT_RUNS_PER_TASK = 100;

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
  version: number;
}

interface AgentRunsTaskState {
  runs: Map<string, AgentRunRecord>;
  version: number;
}

interface MutationResult {
  changed: boolean;
}

const defaultSnapshot: AgentRunsSnapshot = {
  runs: [],
  runsById: {},
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
      version: 0,
    };
    taskStates.set(taskId, state);
    snapshotCache.set(taskId, buildSnapshot(state));
  } else {
    touchTaskState(taskId, state);
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
    runs: new Map(state.runs),
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
    const evicted = pruneTaskStates();
    if (evicted) {
      emit();
    }
    return;
  }
  pruneRuns(draft);
  draft.version = current.version + 1;
  touchTaskState(taskId, draft);
  snapshotCache.set(taskId, buildSnapshot(draft));
  pruneTaskStates();
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
    const terminal = isAgentRunTerminalStatus(projection.status);
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
    touchRun(draft, next);
    return { changed: true };
  });
  if (isAgentRunTerminalStatus(projection.status)) {
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
  payload: unknown,
  sequenceHint?: number,
): boolean {
  const streamPayload = readAgentRunStreamPayload(payload);
  if (!streamPayload) {
    return false;
  }
  const identity = readAgentRunActivityIdentity(taskId, streamPayload);
  if (!identity || identity.internalOnly) {
    return false;
  }
  const projection = readAgentRunLifecycleProjection(streamPayload);
  if (projection) {
    applyAgentRunLifecycleUpdate(
      taskId,
      projection,
      readStreamSequence(streamPayload, sequenceHint),
    );
    return projection.task_id === taskId;
  }
  const sequence = readStreamSequence(streamPayload, sequenceHint);
  mutateTaskState(taskId, draft => {
    const existing = draft.runs.get(identity.agentRunId);
    const nextRun = existing ?? emptyRunFromActivity(identity);
    const activity = upsertActivity(nextRun.activity, {
      taskId,
      agentRunId: identity.agentRunId,
      sequence,
      payload: streamPayload,
      receivedAt: Date.now(),
    });
    if (activity === nextRun.activity) {
      return { changed: false };
    }
    touchRun(draft, {
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
    for (const [agentRunId, run] of Array.from(draft.runs.entries())) {
      if (localRunsById.has(agentRunId) || isAgentRunTerminalStatus(run.status)) {
        continue;
      }
      touchRun(draft, {
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

export function getAgentRunParentGroupingKey(run: AgentRunRecord): string {
  return run.parentRunId ?? run.parentTurnId;
}

export function getAgentRunSnapshot(taskId: number | null | undefined): AgentRunsSnapshot {
  if (typeof taskId !== "number" || !Number.isFinite(taskId) || taskId <= 0) {
    return defaultSnapshot;
  }
  const cached = snapshotCache.get(taskId);
  if (cached) {
    const state = taskStates.get(taskId);
    if (state) {
      touchTaskState(taskId, state);
    }
    return cached;
  }
  const state = taskStates.get(taskId);
  if (!state) {
    return defaultSnapshot;
  }
  const snapshot = buildSnapshot(state);
  touchTaskState(taskId, state);
  snapshotCache.set(taskId, snapshot);
  return snapshot;
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

function touchTaskState(taskId: number, state: AgentRunsTaskState): void {
  taskStates.delete(taskId);
  taskStates.set(taskId, state);
}

function touchRun(state: AgentRunsTaskState, run: AgentRunRecord): void {
  state.runs.delete(run.agentRunId);
  state.runs.set(run.agentRunId, run);
}

function pruneRuns(state: AgentRunsTaskState): void {
  while (state.runs.size > MAX_AGENT_RUNS_PER_TASK) {
    const evictionPool = Array.from(state.runs.values());
    evictionPool.sort(compareRunEvictionPriority);
    const evicted = evictionPool[0];
    if (!evicted) {
      return;
    }
    state.runs.delete(evicted.agentRunId);
  }
}

function compareRunEvictionPriority(a: AgentRunRecord, b: AgentRunRecord): number {
  return (
    Number(!isAgentRunTerminalStatus(a.status)) -
    Number(!isAgentRunTerminalStatus(b.status))
  );
}

function pruneTaskStates(): boolean {
  let changed = false;
  while (taskStates.size > MAX_AGENT_RUN_TASK_STATES) {
    let candidate: [number, AgentRunsTaskState] | null = null;
    let candidatePriority = Number.MAX_SAFE_INTEGER;
    for (const entry of taskStates.entries()) {
      const priority = taskEvictionPriority(entry[1]);
      if (priority < candidatePriority) {
        candidate = entry;
        candidatePriority = priority;
      }
    }
    if (!candidate) {
      break;
    }
    taskStates.delete(candidate[0]);
    snapshotCache.delete(candidate[0]);
    changed = true;
  }
  return changed;
}

function taskEvictionPriority(state: AgentRunsTaskState): number {
  const terminalOnly =
    state.runs.size === 0 ||
    Array.from(state.runs.values()).every(run => isAgentRunTerminalStatus(run.status));
  return terminalOnly ? 0 : 1;
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
