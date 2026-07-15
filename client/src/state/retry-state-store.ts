/**
 * Task-scoped store for in-flight checkpoint retry lifecycle state.
 *
 * Responsibilities:
 * - hold the canonical retry state for each (taskId, turnId | workflowId) pair
 *   so chat surfaces can disable the retry CTA while the backend retry
 *   worker is active and re-enable it deterministically on terminal states
 * - surface a single, task-local source of truth that survives component
 *   re-renders (mutation success in ``useGraphRetry`` writes here, and stream
 *   event handling updates the same record)
 * - keep state strictly task-local (writes for task A never affect task B)
 *
 * Lifecycle states:
 *   ``accepted | started | retrying | waiting_for_human | completed |
 *   declined | failed | cancelled``.
 *
 * "In-flight" lifecycle states keep the retry button disabled:
 *   ``accepted``, ``started``, ``retrying``, ``waiting_for_human``.
 *
 * Terminal states settle the entry:
 *   ``completed`` and ``cancelled`` keep the CTA disabled (no further retry
 *   from the failed bubble); ``failed`` clears in-flight so the bubble can
 *   re-enable when the backend still marks the message ``retryable`` (i.e.
 *   the retry budget has not been exhausted).
 *
 * Keying strategy:
 *   The backend only guarantees a stable ``turn_id`` for retryable failed
 *   turns; ``workflow_id`` is added on the retry-claim response. We key by
 *   ``taskId`` + ``turnId`` since that is what ``MessageBubble`` resolves
 *   from message metadata. ``workflowId`` is stored on the entry but not
 *   used for lookup so that a write before workflow_id arrives can still
 *   be matched on turn_id.
 */
import { useSyncExternalStore } from "react";

export const RETRY_LIFECYCLE_STATES = [
  "accepted",
  "started",
  "retrying",
  "waiting_for_human",
  "completed",
  "declined",
  "failed",
  "cancelled",
] as const;

export type RetryLifecycleState = (typeof RETRY_LIFECYCLE_STATES)[number];

const RETRY_LIFECYCLE_STATE_SET: ReadonlySet<string> = new Set(
  RETRY_LIFECYCLE_STATES,
);

export interface TaskRetryStateEntry {
  taskId: number;
  turnId: string;
  workflowId: number | null;
  state: RetryLifecycleState;
  retryAttempt: number | null;
  retryMaxAttempts: number | null;
  inFlight: boolean;
  /**
   * Monotonic update timestamp (epoch ms). Used to ignore out-of-order
   * stream events that try to revive a terminal state.
   */
  updatedAt: number;
}

const IN_FLIGHT_STATES: ReadonlySet<RetryLifecycleState> = new Set([
  "accepted",
  "started",
  "retrying",
  "waiting_for_human",
]);

export function readRetryLifecycleState(
  value: unknown,
): RetryLifecycleState | null {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  if (RETRY_LIFECYCLE_STATE_SET.has(normalized)) {
    return normalized as RetryLifecycleState;
  }
  return null;
}

function isInFlightState(state: RetryLifecycleState): boolean {
  return IN_FLIGHT_STATES.has(state);
}

function makeKey(taskId: number, turnId: string): string {
  return `${taskId}::${turnId}`;
}

const entriesByKey = new Map<string, TaskRetryStateEntry>();
const listeners = new Set<() => void>();
let version = 0;

function emit(): void {
  version += 1;
  for (const listener of listeners) {
    try {
      listener();
    } catch {
      // listeners must not throw; swallow to keep store invariants stable.
    }
  }
}

export interface RetryStateUpdate {
  taskId: number;
  turnId: string;
  workflowId?: number | null;
  state: RetryLifecycleState;
  retryAttempt?: number | null;
  retryMaxAttempts?: number | null;
}

/**
 * Apply a retry lifecycle update for a (taskId, turnId) pair.
 *
 * Behavior:
 * - Unknown taskId/turnId pairs initialize a new entry.
 * - ``inFlight`` is computed from the lifecycle state (in-flight vs
 *   terminal); callers cannot fight the lifecycle invariant.
 * - Once an entry has reached a terminal state, additional in-flight
 *   updates for the same key are ignored unless the new state is also a
 *   recognized lifecycle progression (e.g. a server-issued
 *   ``waiting_for_human`` arriving after a stale ``failed`` is allowed
 *   only if the server explicitly tells us so by sending a non-terminal
 *   state — to keep the contract simple here, terminal entries are
 *   sticky for the same workflow_id; if a NEW retry begins on the same
 *   turn, the caller should pass the fresh attempt and we replace).
 */
export function applyRetryStateUpdate(update: RetryStateUpdate): void {
  if (!Number.isFinite(update.taskId) || update.taskId <= 0) {
    return;
  }
  const turnId = typeof update.turnId === "string" ? update.turnId.trim() : "";
  if (!turnId) {
    return;
  }
  const key = makeKey(update.taskId, turnId);
  const existing = entriesByKey.get(key);
  const nextWorkflowId =
    typeof update.workflowId === "number" && Number.isFinite(update.workflowId)
      ? update.workflowId
      : update.workflowId === null
        ? null
        : (existing?.workflowId ?? null);

  // Sticky-terminal invariant for ``completed`` / ``cancelled``:
  // once a retry has fully settled into a no-further-CTA terminal
  // state, late-arriving stream events for the SAME workflow_id
  // must not un-terminate it. A NEW workflow_id (i.e. a follow-up
  // attempt) is allowed to replace the entry.
  //
  // ``failed`` is intentionally NOT sticky: the backend retry budget
  // permits more attempts when the message is still server-marked
  // ``retryable``, and the user can click retry again — at click
  // time we don't yet know the new workflow_id, so we must allow the
  // ``failed → accepted`` optimistic transition.
  const existingIsHardTerminal =
    existing != null &&
    (existing.state === "completed" || existing.state === "cancelled");
  const isFreshWorkflow =
    existingIsHardTerminal &&
    typeof nextWorkflowId === "number" &&
    typeof existing.workflowId === "number" &&
    nextWorkflowId !== existing.workflowId;

  if (existingIsHardTerminal && !isFreshWorkflow) {
    if (existing.state !== update.state) {
      return;
    }
  }

  const next: TaskRetryStateEntry = {
    taskId: update.taskId,
    turnId,
    workflowId: nextWorkflowId,
    state: update.state,
    retryAttempt:
      typeof update.retryAttempt === "number" && Number.isFinite(update.retryAttempt)
        ? update.retryAttempt
        : update.retryAttempt === null
          ? null
          : (existing?.retryAttempt ?? null),
    retryMaxAttempts:
      typeof update.retryMaxAttempts === "number" && Number.isFinite(update.retryMaxAttempts)
        ? update.retryMaxAttempts
        : update.retryMaxAttempts === null
          ? null
          : (existing?.retryMaxAttempts ?? null),
    inFlight: isInFlightState(update.state),
    updatedAt: Date.now(),
  };

  if (
    existing &&
    existing.state === next.state &&
    existing.workflowId === next.workflowId &&
    existing.retryAttempt === next.retryAttempt &&
    existing.retryMaxAttempts === next.retryMaxAttempts &&
    existing.inFlight === next.inFlight
  ) {
    // No-op write: avoid spurious re-render fan-out.
    return;
  }

  entriesByKey.set(key, next);
  emit();
}

/**
 * Return the retry state for a (taskId, turnId) pair, or null if no
 * retry attempt has been observed for it yet.
 *
 * Reads are O(1) and stable across renders; the returned object is the
 * exact stored entry (treat as readonly).
 */
export function getRetryStateForTurn(
  taskId: number | null | undefined,
  turnId: string | null | undefined,
): TaskRetryStateEntry | null {
  if (typeof taskId !== "number" || !Number.isFinite(taskId) || taskId <= 0) {
    return null;
  }
  const trimmedTurnId = typeof turnId === "string" ? turnId.trim() : "";
  if (!trimmedTurnId) {
    return null;
  }
  return entriesByKey.get(makeKey(taskId, trimmedTurnId)) ?? null;
}

/**
 * Clear all retry state for the given task. Intended for use when the
 * task is unmounted or the user switches contexts; not normally
 * required during the lifecycle since terminal states settle entries
 * deterministically.
 */
export function clearRetryStateForTask(taskId: number): void {
  if (typeof taskId !== "number" || !Number.isFinite(taskId) || taskId <= 0) {
    return;
  }
  let mutated = false;
  for (const [key, entry] of entriesByKey) {
    if (entry.taskId === taskId) {
      entriesByKey.delete(key);
      mutated = true;
    }
  }
  if (mutated) {
    emit();
  }
}

/**
 * Test-only reset hook. Not exported as part of the runtime contract;
 * used by vitest setUp/tearDown to keep cases independent.
 */
export function __resetRetryStateStoreForTest(): void {
  entriesByKey.clear();
  version += 1;
  for (const listener of listeners) {
    try {
      listener();
    } catch {
      // ignore
    }
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * React hook that returns the retry state for a given (taskId, turnId)
 * pair. Returns ``null`` until the first retry attempt is observed for
 * that pair.
 */
export function useTaskRetryState(
  taskId: number | null | undefined,
  turnId: string | null | undefined,
): TaskRetryStateEntry | null {
  return useSyncExternalStore(
    subscribe,
    () => getRetryStateForTurn(taskId, turnId),
    () => null,
  );
}

/**
 * React hook that returns the current store version. Useful for
 * components that need a single subscription covering many entries
 * (e.g. when iterating a transcript). Most callers should prefer
 * ``useTaskRetryState`` for per-message access.
 */
export function useRetryStateStoreVersion(): number {
  return useSyncExternalStore(
    subscribe,
    () => version,
    () => 0,
  );
}
