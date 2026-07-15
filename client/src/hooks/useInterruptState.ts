/**
 * Hook for querying interrupt state from the backend API.
 *
 * Interrupt tracking is stream-first:
 * - live updates from graph_interrupt SSE events
 * - snapshot hydration/reconciliation from GET /interrupt
 *
 * Replaces localStorage-based usePendingInterrupt hook.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import type { GraphInterruptEventDetail, InterruptPayload } from "@/types/hitl";
import { apiFetch } from "@/lib/api-config";
import { useTaskStreamSnapshot } from "@/state/chat-stream-store";

const RECONCILE_COOLDOWN_MS = 1500;
const INTERRUPT_CLEAR_GRACE_MS = 4000;
const NO_INTERRUPT_CONFIRMATIONS_REQUIRED = 2;

/**
 * Response from GET /api/tasks/{taskId}/interrupt
 */
interface InterruptApiResponse {
  has_interrupt: boolean;
  task_id: number;
  task_missing?: boolean;
  interrupt_id?: string;
  checkpoint_id?: string | null;
  thread_id?: string;
  graph_name?: string;
  interrupt_type?: "tool_approval" | "plan_review" | "clarify_request";
  payload?: InterruptPayload;
  resumable?: boolean;
}

/**
 * Fetch interrupt state from backend API.
 */
async function fetchInterruptState(taskId: number): Promise<InterruptApiResponse> {
  const response = await apiFetch(`/api/tasks/${taskId}/interrupt`, {
    method: "GET",
  });

  if (!response.ok) {
    if (response.status === 404) {
      // Task no longer exists; clients can stop reconcile attempts.
      return { has_interrupt: false, task_id: taskId, task_missing: true };
    }
    // Return no interrupt on auth errors to avoid breaking the UI state machine.
    if (response.status === 401) {
      return { has_interrupt: false, task_id: taskId };
    }
    throw new Error(`Failed to fetch interrupt state: ${response.status}`);
  }

  return response.json();
}

/**
 * Convert API response to GraphInterruptEventDetail format.
 */
function toEventDetail(response: InterruptApiResponse): GraphInterruptEventDetail | null {
  if (!response.has_interrupt || !response.payload) {
    return null;
  }
  const payloadInterruptId =
    typeof (response.payload as { interrupt_id?: unknown }).interrupt_id === "string"
      ? ((response.payload as { interrupt_id?: string }).interrupt_id as string)
      : null;
  const interruptId =
    (typeof response.interrupt_id === "string" && response.interrupt_id) || payloadInterruptId;
  if (!interruptId) {
    return null;
  }

  return {
    taskId: response.task_id,
    threadId: response.thread_id || `task-${response.task_id}`,
    interruptId,
    checkpointId: response.checkpoint_id ?? null,
    interruptType: response.interrupt_type || "tool_approval",
    graphName: response.graph_name || "simple_tool",
    payload: response.payload,
  };
}

interface UseInterruptStateResult {
  /** Current pending interrupt for the task, or null */
  interrupt: GraphInterruptEventDetail | null;
  /** Whether the query is currently loading */
  isLoading: boolean;
  /** Whether there was an error fetching interrupt state */
  isError: boolean;
  /** Error message if any */
  error: Error | null;
  /** Refetch interrupt state from API */
  refetch: () => Promise<void>;
  /** Set/clear local interrupt view state with optional reveal controls. */
  setInterrupt: (
    detail: GraphInterruptEventDetail | null,
    options?: SetInterruptOptions,
  ) => void;
  /** Check if an interrupt exists for this task */
  hasInterrupt: boolean;
}

export interface SetInterruptOptions {
  /**
   * When true and setting non-null detail, allows re-showing an interrupt that
   * was previously dismissed with the same interrupt_id (used for local error recovery).
   */
  allowDismissedReveal?: boolean;
}

interface DismissedInterruptEntry {
  interruptId: string;
  dismissedAt: number;
}

/**
 * Query key factory for interrupt state.
 */
export const interruptStateKeys = {
  all: ["interrupt-state"] as const,
  task: (taskId: number) => [...interruptStateKeys.all, taskId] as const,
};

/**
 * Hook to query interrupt state from backend API.
 *
 * This hook:
 * - Queries backend on mount and task change
 * - Uses SSE graph_interrupt events as the live source during connected sessions
 * - Treats API reads as hydration fallback, not a polling loop
 * - Returns consistent interface with old usePendingInterrupt
 *
 * @param taskId - Current task ID, or null if no task selected
 * @returns Object with interrupt state and management functions
 *
 * @example
 * ```tsx
 * const { interrupt, isLoading, refetch, setInterrupt } = useInterruptState(taskId);
 *
 * // After resume, refetch to verify cleared
 * const handleApprove = async () => {
 *   await resumeGraph();
 *   await refetch();
 * };
 * ```
 */
export function useInterruptState(taskId: number | null): UseInterruptStateResult {
  const [optimisticByTask, setOptimisticByTask] = useState<Partial<Record<number, GraphInterruptEventDetail>>>({});
  const [dismissedByTask, setDismissedByTask] = useState<
    Partial<Record<number, DismissedInterruptEntry>>
  >({});
  const optimisticByTaskRef = useRef<Partial<Record<number, GraphInterruptEventDetail>>>({});
  const noInterruptSnapshotCountRef = useRef<Map<number, number>>(new Map());
  const lastInterruptSeenAtRef = useRef<Map<number, number>>(new Map());
  const connectionSnapshot = useTaskStreamSnapshot(taskId);
  const wasConnectedRef = useRef(false);
  const reconcileInFlightRef = useRef<Map<number, Promise<void>>>(new Map());
  const lastReconcileAtRef = useRef<Map<number, number>>(new Map());
  const queryClient = useQueryClient();

  useEffect(() => {
    optimisticByTaskRef.current = optimisticByTask;
  }, [optimisticByTask]);

  // Snapshot query is read-through only. Reconcile cadence is controlled by explicit triggers.
  const {
    data,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: interruptStateKeys.task(taskId ?? 0),
    queryFn: () => fetchInterruptState(taskId!),
    enabled: false,
    staleTime: 120_000, // Live updates come from stream events; API is hydration fallback.
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 1, // Only retry once on failure
  });

  // Convert API response to event detail
  const apiInterrupt = data ? toEventDetail(data) : null;

  const getInterruptKey = useCallback((detail: GraphInterruptEventDetail | null): string | null => {
    if (!detail || !detail.interruptId) {
      return null;
    }
    return detail.interruptId;
  }, []);

  const optimisticInterrupt = taskId != null ? optimisticByTask[taskId] ?? null : null;
  const dismissedInterrupt = taskId != null ? dismissedByTask[taskId] ?? null : null;
  const dismissedInterruptId = dismissedInterrupt?.interruptId ?? null;
  const rawInterrupt = optimisticInterrupt ?? apiInterrupt;
  const currentInterruptKey = getInterruptKey(rawInterrupt);

  // Only show interrupt if it hasn't been dismissed for the active task.
  const interrupt = (currentInterruptKey && currentInterruptKey === dismissedInterruptId) 
    ? null 
    : rawInterrupt;

  const clearTaskLocalState = useCallback((targetTaskId: number, options?: { clearDismissed?: boolean }) => {
    if (!Number.isFinite(targetTaskId) || targetTaskId <= 0) {
      return;
    }
    const normalizedTaskId = Math.floor(targetTaskId);
    setOptimisticByTask((prev) => {
      if (!(normalizedTaskId in prev)) {
        return prev;
      }
      const next = { ...prev };
      delete next[normalizedTaskId];
      return next;
    });
    noInterruptSnapshotCountRef.current.delete(normalizedTaskId);
    lastInterruptSeenAtRef.current.delete(normalizedTaskId);
    if (options?.clearDismissed) {
      setDismissedByTask((prev) => {
        if (!(normalizedTaskId in prev)) {
          return prev;
        }
        const next = { ...prev };
        delete next[normalizedTaskId];
        return next;
      });
    }
  }, []);

  const reconcileTask = useCallback(
    async (
      targetTaskId: number,
      options?: {
        force?: boolean;
      },
    ) => {
      if (!Number.isFinite(targetTaskId) || targetTaskId <= 0) {
        return;
      }
      const normalizedTaskId = Math.floor(targetTaskId);
      const existing = reconcileInFlightRef.current.get(normalizedTaskId);
      if (existing) {
        return existing;
      }

      const now = Date.now();
      const force = Boolean(options?.force);
      const lastReconcileAt = lastReconcileAtRef.current.get(normalizedTaskId) ?? 0;
      if (!force && now - lastReconcileAt < RECONCILE_COOLDOWN_MS) {
        return;
      }
      lastReconcileAtRef.current.set(normalizedTaskId, now);

      const pending = (async () => {
        const snapshot = await queryClient.fetchQuery({
          queryKey: interruptStateKeys.task(normalizedTaskId),
          queryFn: () => fetchInterruptState(normalizedTaskId),
          staleTime: 0,
        });
        if (!snapshot.has_interrupt) {
          const optimisticInterrupt = optimisticByTaskRef.current[normalizedTaskId] ?? null;
          if (optimisticInterrupt) {
            const nowTs = Date.now();
            const lastSeenAt = lastInterruptSeenAtRef.current.get(normalizedTaskId) ?? 0;
            const withinGraceWindow = nowTs - lastSeenAt < INTERRUPT_CLEAR_GRACE_MS;
            const previousMisses = noInterruptSnapshotCountRef.current.get(normalizedTaskId) ?? 0;
            const nextMisses = previousMisses + 1;
            noInterruptSnapshotCountRef.current.set(normalizedTaskId, nextMisses);

            if (withinGraceWindow || nextMisses < NO_INTERRUPT_CONFIRMATIONS_REQUIRED) {
              return;
            }
          }
          clearTaskLocalState(normalizedTaskId, { clearDismissed: true });
          return;
        }
        noInterruptSnapshotCountRef.current.delete(normalizedTaskId);
        lastInterruptSeenAtRef.current.set(normalizedTaskId, Date.now());
      })().finally(() => {
        reconcileInFlightRef.current.delete(normalizedTaskId);
      });

      reconcileInFlightRef.current.set(normalizedTaskId, pending);
      return pending;
    },
    [clearTaskLocalState, queryClient],
  );

  // Keep local overlays clean whenever authoritative snapshot has no pending interrupt.
  useEffect(() => {
    if (taskId === null || !data) {
      return;
    }
    if (!data.has_interrupt) {
      if (optimisticByTask[taskId]) {
        return;
      }
      clearTaskLocalState(taskId, { clearDismissed: true });
    }
  }, [clearTaskLocalState, data, optimisticByTask, taskId]);

  // Refetch wrapper for mutation callbacks and manual recoveries.
  const refetch = useCallback(async () => {
    if (taskId === null) {
      return;
    }
    await reconcileTask(taskId, { force: true });
  }, [reconcileTask, taskId]);

  // Set interrupt locally from stream events or explicit UI recovery actions.
  // If setting to null, mark current interrupt id as dismissed for that task.
  const setInterrupt = useCallback((
    detail: GraphInterruptEventDetail | null,
    options?: SetInterruptOptions,
  ) => {
    const targetTaskId = detail?.taskId ?? taskId;
    if (targetTaskId == null || !Number.isFinite(targetTaskId) || targetTaskId <= 0) {
      return;
    }
    const normalizedTaskId = Math.floor(targetTaskId);

    if (detail === null) {
      const keyToDismiss = getInterruptKey(
        normalizedTaskId === taskId ? rawInterrupt : optimisticByTask[normalizedTaskId] ?? null,
      );
      if (keyToDismiss) {
        setDismissedByTask((prev) => ({
          ...prev,
          [normalizedTaskId]: {
            interruptId: keyToDismiss,
            dismissedAt: Date.now(),
          },
        }));
      }
      clearTaskLocalState(normalizedTaskId);
      return;
    }

    const nextInterruptId = getInterruptKey(detail);
    lastInterruptSeenAtRef.current.set(normalizedTaskId, Date.now());
    noInterruptSnapshotCountRef.current.delete(normalizedTaskId);
    setOptimisticByTask((prev) => ({
      ...prev,
      [normalizedTaskId]: detail,
    }));
    setDismissedByTask((prev) => {
      const dismissedEntry = prev[normalizedTaskId];
      if (!dismissedEntry) {
        return prev;
      }
      const isSameInterrupt = nextInterruptId === dismissedEntry.interruptId;
      const shouldRevealDismissed = Boolean(options?.allowDismissedReveal);
      if (isSameInterrupt && !shouldRevealDismissed) {
        return prev;
      }
      const next = { ...prev };
      delete next[normalizedTaskId];
      return next;
    });
  }, [clearTaskLocalState, getInterruptKey, optimisticByTask, rawInterrupt, taskId]);

  // One-shot reconcile whenever active task changes.
  useEffect(() => {
    if (taskId === null) {
      wasConnectedRef.current = false;
      return;
    }
    void reconcileTask(taskId, { force: true });
  }, [reconcileTask, taskId]);

  // One-shot foreground reconciliation for the active task.
  useEffect(() => {
    if (taskId === null || typeof document === "undefined" || typeof window === "undefined") {
      return;
    }

    const reconcileCurrentTask = () => {
      void reconcileTask(taskId, { force: true });
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        reconcileCurrentTask();
      }
    };
    const onPageShow = () => {
      reconcileCurrentTask();
    };
    const onOnline = () => {
      reconcileCurrentTask();
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pageshow", onPageShow);
    window.addEventListener("online", onOnline);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pageshow", onPageShow);
      window.removeEventListener("online", onOnline);
    };
  }, [reconcileTask, taskId]);

  // Live interrupt updates are delivered from the runtime stream layer via window events.
  // Consume all task-scoped events so local cache survives task switching without refresh.
  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const handler = (event: Event) => {
      const customEvent = event as CustomEvent<GraphInterruptEventDetail>;
      const detail = customEvent.detail;
      const detailTaskId = Number(detail?.taskId);
      if (!detail || !Number.isFinite(detailTaskId) || detailTaskId <= 0) {
        return;
      }
      setInterrupt({ ...detail, taskId: detailTaskId });
    };

    window.addEventListener("graph-interrupt", handler as EventListener);
    return () => {
      window.removeEventListener("graph-interrupt", handler as EventListener);
    };
  }, [setInterrupt]);

  // Runtime status events preserve interrupt lifecycle transitions (set/clear).
  // Pending/unknown states trigger one-shot snapshot reconciliation.
  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail ?? {};
      const detailTaskId = Number((detail.taskId as number | undefined) ?? (detail.task_id as number | undefined));
      if (!Number.isFinite(detailTaskId) || detailTaskId <= 0) {
        return;
      }
      const state = String(detail.state ?? "").toLowerCase();
      const interruptId =
        typeof detail.interruptId === "string"
          ? detail.interruptId
          : typeof detail.interrupt_id === "string"
            ? detail.interrupt_id
            : null;

      if (state === "none" || state === "cleared" || state === "resolved" || state === "idle") {
        const optimisticInterrupt = optimisticByTaskRef.current[detailTaskId] ?? null;
        const activeInterruptForTask =
          detailTaskId === taskId ? (rawInterrupt ?? optimisticInterrupt) : optimisticInterrupt;
        const activeInterruptId = getInterruptKey(activeInterruptForTask);

        if (interruptId && activeInterruptId && interruptId !== activeInterruptId) {
          return;
        }

        if (interruptId) {
          setDismissedByTask((prev) => ({
            ...prev,
            [detailTaskId]: {
              interruptId,
              dismissedAt: Date.now(),
            },
          }));
          clearTaskLocalState(detailTaskId);
        } else {
          // Ignore repeated idle/cleared broadcasts when there is no active interrupt to reconcile.
          if (!activeInterruptForTask) {
            return;
          }
          void reconcileTask(detailTaskId, { force: false });
        }
        return;
      }
      void reconcileTask(detailTaskId, { force: false });
    };
    window.addEventListener("task-interrupt-state", handler as EventListener);
    return () => {
      window.removeEventListener("task-interrupt-state", handler as EventListener);
    };
  }, [clearTaskLocalState, getInterruptKey, rawInterrupt, reconcileTask, taskId]);

  // Reconcile once per disconnected->connected transition for the active task.
  useEffect(() => {
    if (taskId === null) {
      wasConnectedRef.current = false;
      return;
    }
    const isConnected = Boolean(connectionSnapshot.isConnected);
    if (isConnected && !wasConnectedRef.current) {
      void reconcileTask(taskId, { force: true });
    }
    wasConnectedRef.current = isConnected;
  }, [connectionSnapshot.isConnected, reconcileTask, taskId]);

  return {
    interrupt,
    isLoading,
    isError,
    error: error as Error | null,
    refetch,
    setInterrupt,
    hasInterrupt: interrupt !== null,
  };
}

/**
 * Invalidate interrupt state cache for a task.
 * Call this after resume to force refetch.
 */
export function invalidateInterruptState(queryClient: ReturnType<typeof useQueryClient>, taskId: number): void {
  queryClient.invalidateQueries({ queryKey: interruptStateKeys.task(taskId) });
}

export default useInterruptState;
