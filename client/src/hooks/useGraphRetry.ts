/**
 * Checkpoint retry mutation for retryable failed LangGraph assistant turns.
 *
 * Responsibilities:
 * - POST retryable assistant turn metadata to the task-scoped graph retry route.
 * - Surface the canonical retry identity payload (Phase 1 contract) as typed
 *   mutation data so callers can disable the retry CTA until a terminal
 *   lifecycle state arrives (Phase 5 contract).
 * - Keep retry transport separate from interrupt resume semantics.
 *
 * Response semantics (Phase 1 + Phase 5):
 * - 2xx with ``already_in_flight`` is success data, NOT a destructive error.
 * - Terminal-state 409s (retry_exhausted / not_retryable / missing /
 *   invalid_state) still surface as ``Error`` so the UI can show the
 *   appropriate reason instead of silently re-arming the CTA.
 */
import { useMutation } from "@tanstack/react-query";

import { apiRequest } from "@/lib/queryClient";

interface RetryParams {
  taskId: number;
  turnId: string;
  retryMode?: "checkpoint";
  graphName?: string;
}

/**
 * Lifecycle state values the backend may include on a retry-claim response.
 *
 * The full lifecycle set (``accepted | started | retrying |
 * waiting_for_human | completed | declined | failed | cancelled``) belongs to the
 * stream-driven retry-state hook (Task 5.2). The mutation response in
 * practice surfaces the initial transition values (``retrying`` / ``started``)
 * and the duplicate-claim state, which is why this is a typed string
 * union rather than ``string``.
 */
export type RetryMutationLifecycleState =
  | "accepted"
  | "started"
  | "retrying"
  | "waiting_for_human"
  | "completed"
  | "declined"
  | "failed"
  | "cancelled";

/**
 * Canonical retry identity returned by ``POST /api/tasks/{task_id}/graph/retry``.
 *
 * Mirrors the Phase 1 backend contract: the same identity is used by the
 * retry worker, status events, and transcript projection so the UI can
 * narrow on it without parsing free-form detail strings.
 */
export interface RetryMutationResult {
  status: string;
  task_id: number;
  turn_id: string;
  retry_mode: string;
  workflow_id: number;
  checkpoint_id: string | null;
  retry_attempt: number;
  retry_max_attempts: number;
  state: RetryMutationLifecycleState;
  already_in_flight?: boolean;
  graph_name?: string | null;
  identity?: Record<string, unknown> | null;
}

/**
 * Runtime sentinel that mirrors {@link RetryMutationResult} so existing
 * callers can ``import { RetryMutationResult } from "@/hooks/useGraphRetry"``
 * without typeof-only erasure. The Phase 0.4 baseline test relies on the
 * symbol being importable at runtime, not just at type-check time.
 */
export const RetryMutationResult: {
  readonly fields: readonly (keyof RetryMutationResult)[];
} = {
  fields: [
    "status",
    "task_id",
    "turn_id",
    "retry_mode",
    "workflow_id",
    "checkpoint_id",
    "retry_attempt",
    "retry_max_attempts",
    "state",
    "already_in_flight",
    "graph_name",
    "identity",
  ],
} as const;

function readString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function readNumber(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return null;
}

function readNullableString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  if (value === null) {
    return null;
  }
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function normalizeRetryMutationResult(
  raw: unknown,
  fallback: { taskId: number; turnId: string; retryMode: string; graphName?: string | null },
): RetryMutationResult {
  const record = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;

  const status = readString(record, "status") ?? "retrying";
  const taskId = readNumber(record, "task_id") ?? fallback.taskId;
  const turnId = readString(record, "turn_id") ?? fallback.turnId;
  const retryMode = readString(record, "retry_mode") ?? fallback.retryMode;
  const workflowId = readNumber(record, "workflow_id") ?? 0;
  const checkpointId = readNullableString(record, "checkpoint_id");
  const retryAttempt = readNumber(record, "retry_attempt") ?? 0;
  const retryMaxAttempts = readNumber(record, "retry_max_attempts") ?? 0;
  const stateRaw = readString(record, "state");
  const state: RetryMutationLifecycleState = (
    stateRaw === "accepted" ||
    stateRaw === "started" ||
    stateRaw === "retrying" ||
    stateRaw === "waiting_for_human" ||
    stateRaw === "completed" ||
    stateRaw === "declined" ||
    stateRaw === "failed" ||
    stateRaw === "cancelled"
  )
    ? stateRaw
    : "retrying";
  const alreadyInFlight =
    typeof record.already_in_flight === "boolean" ? record.already_in_flight : undefined;
  const graphName =
    readNullableString(record, "graph_name") ?? fallback.graphName ?? null;
  const identity =
    record.identity && typeof record.identity === "object" && !Array.isArray(record.identity)
      ? (record.identity as Record<string, unknown>)
      : null;

  return {
    status,
    task_id: taskId,
    turn_id: turnId,
    retry_mode: retryMode,
    workflow_id: workflowId,
    checkpoint_id: checkpointId,
    retry_attempt: retryAttempt,
    retry_max_attempts: retryMaxAttempts,
    state,
    already_in_flight: alreadyInFlight,
    graph_name: graphName,
    identity,
  };
}

export function useGraphRetry() {
  return useMutation<RetryMutationResult, Error, RetryParams>({
    mutationFn: async ({ taskId, turnId, retryMode = "checkpoint", graphName }: RetryParams) => {
      const canonicalTurnId = typeof turnId === "string" ? turnId.trim() : "";
      if (!canonicalTurnId) {
        throw new Error("turn_id is required");
      }

      const payload: Record<string, unknown> = {
        turn_id: canonicalTurnId,
        retry_mode: retryMode,
      };
      if (graphName) {
        payload.graph_name = graphName;
      }

      const res = await apiRequest("POST", `/api/tasks/${taskId}/graph/retry`, payload);
      if (!res.ok) {
        const rawBody = await res.text();
        let detail: string | null = null;
        if (rawBody) {
          try {
            const parsed = JSON.parse(rawBody) as { detail?: unknown };
            if (typeof parsed.detail === "string" && parsed.detail.trim()) {
              detail = parsed.detail.trim();
            }
          } catch {
            detail = rawBody.trim() || null;
          }
        }
        throw new Error(detail || `Failed to retry graph (${res.status})`);
      }

      const raw = await res.json();
      return normalizeRetryMutationResult(raw, {
        taskId,
        turnId: canonicalTurnId,
        retryMode,
        graphName: graphName ?? null,
      });
    },
  });
}
