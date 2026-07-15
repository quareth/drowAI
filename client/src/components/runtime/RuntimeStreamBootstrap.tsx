/**
 * App-shell runtime stream bootstrap.
 *
 * Keeps multiplex websocket lifecycle independent from chat panel mounts by
 * running subscription planning at the application shell level.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/hooks/use-auth";
import { useMultiTaskStreamManager } from "@/hooks/useMultiTaskStreamManager";
import { useActiveChatTaskId } from "@/state/active-chat-task-store";
import { computeDesiredTaskSubscriptions } from "@/services/runtime_stream/TaskSubscriptionPlanner";
import { featureFlags } from "@/config/feature-flags";
import type { Task } from "@/types";

function resolveRuntimeStreamSubscriptionBudget(): number {
  const raw = Number.parseInt(String(import.meta.env.VITE_REASONING_WS_MAX_SUBSCRIPTIONS ?? "3"), 10);
  if (!Number.isFinite(raw) || raw <= 0) {
    return 3;
  }
  return raw;
}

const DEFAULT_RUNTIME_STREAM_SUBSCRIPTION_BUDGET = resolveRuntimeStreamSubscriptionBudget();

function normalizeTaskStatus(status: unknown): string {
  return typeof status === "string" ? status.toLowerCase() : "";
}

function isRuntimeStreamEligibleStatus(status: unknown): boolean {
  const normalized = normalizeTaskStatus(status);
  return normalized === "running" || normalized === "waiting_for_human";
}

function isActiveChatRuntimeStreamEligibleStatus(status: unknown): boolean {
  const normalized = normalizeTaskStatus(status);
  return (
    normalized === "queued" ||
    normalized === "starting" ||
    isRuntimeStreamEligibleStatus(normalized)
  );
}

export function RuntimeStreamBootstrap() {
  const { user } = useAuth();
  const activeChatTaskId = useActiveChatTaskId();
  const enabled = featureFlags.enableMultiTaskStreamManager && Boolean(user);

  const { data: tasks = [] } = useQuery<Task[]>({
    queryKey: ["/api/tasks/"],
    enabled,
  });

  const taskIds = useMemo(() => {
    const runningOrWaitingTaskIds = tasks.filter((task) => isRuntimeStreamEligibleStatus(task.status)).map((task) => task.id);
    const activeTask =
      typeof activeChatTaskId === "number" ? tasks.find((task) => task.id === activeChatTaskId) ?? null : null;
    const activeRuntimeTaskId =
      activeTask && isActiveChatRuntimeStreamEligibleStatus(activeTask.status) ? activeTask.id : null;
    return computeDesiredTaskSubscriptions({
      runningTaskIds: runningOrWaitingTaskIds,
      activeTaskId: activeRuntimeTaskId,
      maxSubscriptions: DEFAULT_RUNTIME_STREAM_SUBSCRIPTION_BUDGET,
    });
  }, [activeChatTaskId, tasks]);

  useMultiTaskStreamManager({
    taskIds,
    enabled,
  });

  return null;
}

export default RuntimeStreamBootstrap;
