/**
 * Pure task/conversation compaction-gate lifecycle authority.
 *
 * The reducer validates ordered context-window lifecycle packets, keeps the
 * active turn/epoch identity, and prevents stale or mismatched terminal
 * packets from releasing a composer gate. The task/conversation store and
 * chat composer consume this reducer as their shared lifecycle authority.
 */

export const CONTEXT_COMPACTION_LIFECYCLE_STATES = [
  "compacting",
  "completed",
  "failed",
  "cancelled",
] as const;

export type ContextCompactionLifecycleState =
  (typeof CONTEXT_COMPACTION_LIFECYCLE_STATES)[number];

export type ContextCompactionTerminalState = Exclude<
  ContextCompactionLifecycleState,
  "compacting"
>;

export interface ContextCompactionLifecycleEvent {
  taskId: number;
  conversationId: string;
  turnId: string;
  epochId: string;
  state: ContextCompactionLifecycleState;
  sequence: number;
}

export interface ContextCompactionGateState {
  taskId: number;
  conversationId: string;
  turnId: string;
  epochId: string;
  active: boolean;
  terminalState: ContextCompactionTerminalState | null;
  lastSequence: number;
}

const LIFECYCLE_STATE_SET: ReadonlySet<string> = new Set(
  CONTEXT_COMPACTION_LIFECYCLE_STATES,
);

function readPositiveInteger(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const normalized = Math.floor(value);
  return normalized > 0 ? normalized : null;
}

function readSequence(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const normalized = Math.floor(value);
  return normalized >= 0 ? normalized : null;
}

function readRequiredString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
}

function readLifecycleState(value: unknown): ContextCompactionLifecycleState | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  return LIFECYCLE_STATE_SET.has(normalized)
    ? (normalized as ContextCompactionLifecycleState)
    : null;
}

export function normalizeContextCompactionLifecycleEvent(
  source: Record<string, unknown>,
  sequenceHint?: unknown,
): ContextCompactionLifecycleEvent | null {
  const taskId = readPositiveInteger(source.taskId ?? source.task_id);
  const conversationId = readRequiredString(
    source.conversationId ?? source.conversation_id,
  );
  const turnId = readRequiredString(source.turnId ?? source.turn_id);
  const epochId = readRequiredString(source.epochId ?? source.epoch_id);
  const state = readLifecycleState(source.state);
  const sequence = readSequence(source.sequence ?? sequenceHint);
  if (
    taskId === null ||
    conversationId === null ||
    turnId === null ||
    epochId === null ||
    state === null ||
    sequence === null
  ) {
    return null;
  }
  return { taskId, conversationId, turnId, epochId, state, sequence };
}

function sameScope(
  current: ContextCompactionGateState,
  event: ContextCompactionLifecycleEvent,
): boolean {
  return (
    current.taskId === event.taskId &&
    current.conversationId === event.conversationId
  );
}

function sameLifecycle(
  current: ContextCompactionGateState,
  event: ContextCompactionLifecycleEvent,
): boolean {
  return current.turnId === event.turnId && current.epochId === event.epochId;
}

export function reduceContextCompactionGate(
  current: ContextCompactionGateState | null,
  event: ContextCompactionLifecycleEvent,
): ContextCompactionGateState {
  if (current && !sameScope(current, event)) return current;
  if (current && event.sequence <= current.lastSequence) return current;

  if (event.state === "compacting") {
    return {
      taskId: event.taskId,
      conversationId: event.conversationId,
      turnId: event.turnId,
      epochId: event.epochId,
      active: true,
      terminalState: null,
      lastSequence: event.sequence,
    };
  }

  if (current?.active && !sameLifecycle(current, event)) {
    return current;
  }

  return {
    taskId: event.taskId,
    conversationId: event.conversationId,
    turnId: event.turnId,
    epochId: event.epochId,
    active: false,
    terminalState: event.state,
    lastSequence: event.sequence,
  };
}
