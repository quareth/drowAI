/**
 * Runtime notification hook for task-scoped stream events.
 *
 * Responsibilities:
 * - Validate generic task notification browser events.
 * - Update the navbar notification store.
 * - Run category-specific cache refreshes without coupling the store to data APIs.
 */
import { useEffect } from "react";

import { queryClient } from "@/lib/queryClient";
import { onActiveTenantChanged } from "@/lib/tenant-context";
import { useAuth } from "@/hooks/use-auth";
import { addNotification, clearNotifications } from "@/state/notification-store";

export interface RuntimeNotificationDetail {
  taskId: number;
  category: string;
  title: string;
  body: string;
  createdAt: string;
  sequence?: number;
  metadata: Record<string, unknown>;
}

function coercePositiveInt(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return Math.floor(value);
  }
  return null;
}

function coerceString(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export function normalizeRuntimeNotificationDetail(value: unknown): RuntimeNotificationDetail | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const candidate = value as Record<string, unknown>;
  const taskId = coercePositiveInt(candidate.taskId);
  const category = coerceString(candidate.category);
  const title = coerceString(candidate.title);
  if (taskId === null || category === null || title === null) {
    return null;
  }
  const body = coerceString(candidate.body) ?? "";
  const createdAt = coerceString(candidate.createdAt) ?? new Date().toISOString();
  const sequence = coercePositiveInt(candidate.sequence);
  const metadata =
    candidate.metadata && typeof candidate.metadata === "object"
      ? { ...(candidate.metadata as Record<string, unknown>) }
      : {};
  return {
    taskId,
    category,
    title,
    body,
    createdAt,
    sequence: sequence ?? undefined,
    metadata,
  };
}

function notificationId(detail: RuntimeNotificationDetail): string {
  const stableSource =
    coerceString(detail.metadata.ingestionRunId) ??
    coerceString(detail.metadata.sourceExecutionId) ??
    `seq-${detail.sequence ?? detail.createdAt}`;
  return `notification:${detail.taskId}:${detail.category}:${stableSource}`;
}

function refreshCachesForNotification(detail: RuntimeNotificationDetail): void {
  if (detail.category !== "knowledge_delta") {
    return;
  }
  void queryClient.invalidateQueries({ queryKey: ["knowledge"] });
  const engagementId =
    typeof detail.metadata.engagementId === "number" && Number.isFinite(detail.metadata.engagementId)
      ? Math.floor(detail.metadata.engagementId)
      : null;
  if (engagementId !== null) {
    void queryClient.invalidateQueries({ queryKey: ["engagement", String(engagementId)] });
    void queryClient.invalidateQueries({ queryKey: ["engagement", engagementId] });
  }
}

export function useRuntimeNotifications(): void {
  const { user } = useAuth();

  useEffect(() => {
    clearNotifications();
  }, [user?.id]);

  useEffect(() => onActiveTenantChanged(() => {
    clearNotifications();
  }), []);

  useEffect(() => {
    const handleTaskNotification = (event: Event) => {
      const detail = normalizeRuntimeNotificationDetail((event as CustomEvent).detail);
      if (detail === null) {
        return;
      }
      addNotification({
        id: notificationId(detail),
        taskId: detail.taskId,
        category: detail.category,
        title: detail.title,
        body: detail.body,
        createdAt: detail.createdAt,
        metadata: detail.metadata,
      });
      refreshCachesForNotification(detail);
    };

    window.addEventListener("task-notification", handleTaskNotification);
    return () => {
      window.removeEventListener("task-notification", handleTaskNotification);
    };
  }, []);
}
