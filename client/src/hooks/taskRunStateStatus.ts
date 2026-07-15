/**
 * Pure task run status normalization for chat stop eligibility.
 *
 * Responsibility: project backend lifecycle and stream activity into the UI
 * contract used by chat controls.
 */

export type TaskRunState = "idle" | "running" | "waiting_for_human" | "completed" | "declined" | "cancelled" | "failed" | "unknown";

export interface TaskRunStatus {
  state: TaskRunState;
  turnId: string | null;
  cancelRequested: boolean;
  isStreaming: boolean;
  queuedCount: number;
  isActiveGeneration: boolean;
  canStop: boolean;
}

export interface StreamTaskRunStatus extends TaskRunStatus {
  receivedAt: number;
}

export function normalizeRunState(value: unknown): TaskRunState {
  const normalized = typeof value === "string" ? value : "";
  if (
    normalized === "idle" ||
    normalized === "running" ||
    normalized === "waiting_for_human" ||
    normalized === "completed" ||
    normalized === "declined" ||
    normalized === "cancelled" ||
    normalized === "failed"
  ) {
    return normalized;
  }
  return "unknown";
}

export function buildTaskRunStatus({
  state,
  turnId,
  cancelRequested = false,
  isStreaming = false,
  queuedCount = 0,
}: {
  state: TaskRunState;
  turnId: string | null;
  cancelRequested?: boolean;
  isStreaming?: boolean;
  queuedCount?: number;
}): TaskRunStatus {
  // The durable lifecycle row is authoritative for cancellation eligibility.
  // Stream connectivity is presentation telemetry and may disappear while the
  // backend turn remains active (for example during a browser reconnect).
  const isActiveGeneration = state === "running" && Boolean(turnId);
  return {
    state,
    turnId,
    cancelRequested,
    isStreaming,
    queuedCount,
    isActiveGeneration,
    canStop: isActiveGeneration && !cancelRequested,
  };
}

export function taskRunStatusFromApiItem(item: unknown): [number, TaskRunStatus] | null {
  if (!item || typeof item !== "object") {
    return null;
  }
  const raw = item as Record<string, any>;
  const taskId = Number(raw.task_id);
  if (!Number.isFinite(taskId)) {
    return null;
  }
  const run = raw.run && typeof raw.run === "object" ? raw.run as Record<string, any> : {};
  return [
    taskId,
    buildTaskRunStatus({
      state: normalizeRunState(run.state),
      turnId: typeof run.turn_id === "string" ? run.turn_id : null,
      cancelRequested: Boolean(run.cancel_requested),
      isStreaming: Boolean(raw.is_streaming ?? raw.isStreaming),
      queuedCount:
        typeof raw.queued_count === "number"
          ? raw.queued_count
          : typeof raw.queuedCount === "number"
            ? raw.queuedCount
            : 0,
    }),
  ];
}

export function shouldUseStreamOverride(
  base: TaskRunStatus | undefined,
  override: StreamTaskRunStatus | undefined,
  baseUpdatedAt: number,
): override is StreamTaskRunStatus {
  if (!override) {
    return false;
  }
  if (
    base &&
    baseUpdatedAt > override.receivedAt &&
    override.state === "running" &&
    base.state !== "running" &&
    base.state !== "unknown"
  ) {
    return false;
  }
  return true;
}
