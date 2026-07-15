/**
 * Event-driven task run state hook.
 *
 * Hydrates once from batch status API, then applies stream-pushed run_state
 * events to avoid interval polling.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api-config";
import {
  buildTaskRunStatus,
  normalizeRunState,
  shouldUseStreamOverride,
  taskRunStatusFromApiItem,
  type StreamTaskRunStatus,
  type TaskRunStatus,
} from "@/hooks/taskRunStateStatus";

export type { TaskRunState, TaskRunStatus } from "@/hooks/taskRunStateStatus";

interface TaskRunStateEventDetail {
  taskId?: number | null;
  task_id?: number | null;
  state?: string | null;
  run_state?: string | null;
  turnId?: string | null;
  turn_id?: string | null;
  cancelRequested?: boolean | null;
  cancel_requested?: boolean | null;
}

interface StreamingStateEventDetail {
  taskId?: number | null;
  task_id?: number | null;
  isStreaming?: boolean | null;
  is_streaming?: boolean | null;
  queuedCount?: number | null;
  queued_count?: number | null;
}

export function useTaskRunState(taskIds: number[]): Record<number, TaskRunStatus> {
  const uniqueTaskIds = useMemo(
    () => Array.from(new Set(taskIds.filter((id) => Number.isFinite(id) && id > 0))),
    [taskIds],
  );

  const runStateQuery = useQuery({
    queryKey: ["task-run-state-batch", uniqueTaskIds],
    enabled: uniqueTaskIds.length > 0,
    queryFn: async (): Promise<Record<number, TaskRunStatus>> => {
      const params = uniqueTaskIds.map((id) => `task_ids=${encodeURIComponent(String(id))}`).join("&");
      const response = await apiFetch(`/api/interactive-runs/statuses?${params}`);
      if (!response.ok) {
        const fallback: Record<number, TaskRunStatus> = {};
        for (const taskId of uniqueTaskIds) {
          fallback[taskId] = buildTaskRunStatus({ state: "unknown", turnId: null });
        }
        return fallback;
      }
      const payload = await response.json().catch(() => null as any);
      const items = Array.isArray(payload?.tasks) ? payload.tasks : [];
      const mapped: Record<number, TaskRunStatus> = {};
      for (const item of items) {
        const parsed = taskRunStatusFromApiItem(item);
        if (!parsed) continue;
        const [taskId, status] = parsed;
        mapped[taskId] = status;
      }
      for (const taskId of uniqueTaskIds) {
        if (!mapped[taskId]) {
          mapped[taskId] = buildTaskRunStatus({ state: "idle", turnId: null });
        }
      }
      return mapped;
    },
    staleTime: 15000,
    refetchOnWindowFocus: false,
  });

  const [streamStateOverrides, setStreamStateOverrides] = useState<Record<number, StreamTaskRunStatus>>({});
  const queryDataRef = useRef<Record<number, TaskRunStatus>>({});
  const streamStateOverridesRef = useRef<Record<number, StreamTaskRunStatus>>({});
  queryDataRef.current = runStateQuery.data ?? {};
  streamStateOverridesRef.current = streamStateOverrides;

  useEffect(() => {
    const allowed = new Set(uniqueTaskIds);
    setStreamStateOverrides((prev) => {
      const next: Record<number, StreamTaskRunStatus> = {};
      let changed = false;
      for (const [taskIdRaw, value] of Object.entries(prev)) {
        const taskId = Number(taskIdRaw);
        if (!allowed.has(taskId)) {
          changed = true;
          continue;
        }
        next[taskId] = value;
      }
      if (changed) {
        streamStateOverridesRef.current = next;
        return next;
      }
      return prev;
    });
  }, [uniqueTaskIds]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return () => undefined;
    }
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<TaskRunStateEventDetail>).detail;
      const taskId = Number(detail?.taskId ?? detail?.task_id);
      if (!Number.isFinite(taskId) || taskId <= 0) {
        return;
      }
      const current = streamStateOverridesRef.current[taskId] ?? queryDataRef.current[taskId];
      const next = buildTaskRunStatus({
        state: normalizeRunState(detail?.state ?? detail?.run_state),
        turnId:
          typeof detail?.turnId === "string"
            ? detail.turnId
            : typeof detail?.turn_id === "string"
              ? detail.turn_id
              : null,
        cancelRequested: Boolean(detail?.cancelRequested ?? detail?.cancel_requested),
        isStreaming: current?.isStreaming ?? false,
        queuedCount: current?.queuedCount ?? 0,
      });
      const receivedAt = Date.now();
      setStreamStateOverrides((prev) => {
        const current = prev[taskId];
        if (
          current &&
          current.state === next.state &&
          current.turnId === next.turnId &&
          current.cancelRequested === next.cancelRequested
        ) {
          return prev;
        }
        const nextOverrides = { ...prev, [taskId]: { ...next, receivedAt } };
        streamStateOverridesRef.current = nextOverrides;
        return nextOverrides;
      });
    };
    window.addEventListener("task-run-state", handler as EventListener);
    return () => {
      window.removeEventListener("task-run-state", handler as EventListener);
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") {
      return () => undefined;
    }
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<StreamingStateEventDetail>).detail;
      const taskId = Number(detail?.taskId ?? detail?.task_id);
      if (!Number.isFinite(taskId) || taskId <= 0) {
        return;
      }
      const isStreamingRaw = detail?.isStreaming ?? detail?.is_streaming;
      const queuedCountRaw = detail?.queuedCount ?? detail?.queued_count;
      const current = streamStateOverridesRef.current[taskId] ?? queryDataRef.current[taskId];
      const next = buildTaskRunStatus({
        state: current?.state ?? "idle",
        turnId: current?.turnId ?? null,
        cancelRequested: current?.cancelRequested ?? false,
        isStreaming: typeof isStreamingRaw === "boolean" ? isStreamingRaw : current?.isStreaming ?? false,
        queuedCount: typeof queuedCountRaw === "number" ? queuedCountRaw : current?.queuedCount ?? 0,
      });
      const receivedAt = Date.now();
      setStreamStateOverrides((prev) => {
        const currentOverride = prev[taskId];
        if (
          currentOverride &&
          currentOverride.state === next.state &&
          currentOverride.turnId === next.turnId &&
          currentOverride.cancelRequested === next.cancelRequested &&
          currentOverride.isStreaming === next.isStreaming &&
          currentOverride.queuedCount === next.queuedCount
        ) {
          return prev;
        }
        const nextOverrides = { ...prev, [taskId]: { ...next, receivedAt } };
        streamStateOverridesRef.current = nextOverrides;
        return nextOverrides;
      });
    };
    window.addEventListener("llm-streaming", handler as EventListener);
    return () => {
      window.removeEventListener("llm-streaming", handler as EventListener);
    };
  }, []);

  return useMemo(() => {
    if (uniqueTaskIds.length === 0) return {};
    const base = runStateQuery.data ?? {};
    const resolved: Record<number, TaskRunStatus> = {};
    for (const taskId of uniqueTaskIds) {
      const baseStatus = base[taskId];
      const override = streamStateOverrides[taskId];
      resolved[taskId] = shouldUseStreamOverride(baseStatus, override, runStateQuery.dataUpdatedAt)
        ? override
        : (baseStatus ?? buildTaskRunStatus({ state: "idle", turnId: null }));
    }
    return resolved;
  }, [runStateQuery.data, streamStateOverrides, uniqueTaskIds]);
}

export default useTaskRunState;
