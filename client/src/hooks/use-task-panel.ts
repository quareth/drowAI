/**
 * Shared TaskPanel hooks for view-state orchestration and task mutations.
 *
 * Responsibilities:
 * - Keep TaskPanel view state/persistence logic out of the render component.
 * - Keep task action/delete transport, cache invalidation, and store cleanup in one place.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { invalidateEngagementKnowledgeQueries, useEngagements } from "@/hooks/use-engagement-knowledge";
import {
  ACTIVE_TASK_STATUSES,
  EngagementGroup,
  groupTasksByEngagement,
  normalizeTaskPanelNameFilter,
} from "@/lib/task-grouping";
import { apiRequest, queryClient } from "@/lib/queryClient";
import { responseToError } from "@/lib/response-error";
import type { Task } from "@/types";
import { clearTaskState } from "@/state/chat-stream-store";
import { clearChatSession } from "@/state/chat-session-store";
import { getActiveChatTaskId, setActiveChatTaskId } from "@/state/active-chat-task-store";

export type TaskPanelViewMode = "grouped" | "flat";

const VIEW_MODE_KEY = "drowai:task-panel:view-mode";

function safeLocalStorageGetItem(key: string): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  const storage = window.localStorage as Storage | undefined;
  if (!storage || typeof storage.getItem !== "function") {
    return null;
  }
  return storage.getItem(key);
}

function safeLocalStorageSetItem(key: string, value: string): void {
  if (typeof window === "undefined") {
    return;
  }
  const storage = window.localStorage as Storage | undefined;
  if (!storage || typeof storage.setItem !== "function") {
    return;
  }
  storage.setItem(key, value);
}

export function useTaskPanelViewState(filteredTasks: Task[]) {
  const [viewMode, setViewMode] = useState<TaskPanelViewMode>(() => {
    const raw = safeLocalStorageGetItem(VIEW_MODE_KEY);
    return raw === "flat" ? "flat" : "grouped";
  });
  const [expandedEngagements, setExpandedEngagements] = useState<Set<number>>(new Set());
  const [showArchivedEngagements, setShowArchivedEngagements] = useState(false);
  const seenActiveTaskIds = useRef<Set<number>>(new Set());

  useEffect(() => {
    safeLocalStorageSetItem(VIEW_MODE_KEY, viewMode);
  }, [viewMode]);

  useEffect(() => {
    if (filteredTasks.length === 0) {
      return;
    }
    const newActiveGroupIds = new Set<number>();
    for (const task of filteredTasks) {
      if (!ACTIVE_TASK_STATUSES.has(task.status) || seenActiveTaskIds.current.has(task.id)) {
        continue;
      }
      seenActiveTaskIds.current.add(task.id);
      if (task.engagement_id != null) {
        newActiveGroupIds.add(task.engagement_id);
      }
    }
    if (newActiveGroupIds.size === 0) {
      return;
    }
    setExpandedEngagements((previous) => {
      const next = new Set(previous);
      for (const id of newActiveGroupIds) {
        next.add(id);
      }
      return next;
    });
  }, [filteredTasks]);

  const toggleEngagementExpanded = useCallback((id: number) => {
    setExpandedEngagements((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  return {
    viewMode,
    setViewMode,
    expandedEngagements,
    toggleEngagementExpanded,
    showArchivedEngagements,
    setShowArchivedEngagements,
  };
}

interface UseTaskPanelEngagementGroupsOptions {
  filteredTasks: Task[];
  searchQuery: string;
  showArchivedEngagements: boolean;
}

export function useTaskPanelEngagementGroups({
  filteredTasks,
  searchQuery,
  showArchivedEngagements,
}: UseTaskPanelEngagementGroupsOptions) {
  const { data: engagementCatalog } = useEngagements({
    limit: 100,
    status: "all",
  });

  const engagementStatusById = useMemo(
    () =>
      new Map<number, string | null>(
        (engagementCatalog?.items ?? []).map((engagement) => [engagement.id, engagement.status]),
      ),
    [engagementCatalog?.items],
  );

  const engagementGroups = useMemo(() => {
    const groupedByTasks = groupTasksByEngagement(filteredTasks, engagementStatusById);
    const knownEngagements = engagementCatalog?.items ?? [];
    if (knownEngagements.length === 0) {
      return groupedByTasks;
    }

    const existingIds = new Set(
      groupedByTasks
        .map((group) => group.engagementId)
        .filter((id): id is number => id !== null),
    );

    const normalizedSearch = normalizeTaskPanelNameFilter(searchQuery);
    const missingGroups: EngagementGroup[] = knownEngagements
      .filter((engagement) => !existingIds.has(engagement.id))
      .filter((engagement) => showArchivedEngagements || engagement.status !== "archived")
      .filter((engagement) =>
        normalizedSearch.length === 0
          ? true
          : engagement.name.toLowerCase().includes(normalizedSearch),
      )
      .map((engagement) => ({
        engagementId: engagement.id,
        engagementName: engagement.name,
        engagementStatus: engagement.status,
        tasks: [],
        statusSummary: "0 tasks",
      }));

    if (missingGroups.length === 0) {
      return groupedByTasks;
    }

    return [...groupedByTasks, ...missingGroups];
  }, [engagementCatalog?.items, engagementStatusById, filteredTasks, searchQuery, showArchivedEngagements]);

  return {
    engagementCatalog,
    engagementGroups,
  };
}

interface UseTaskPanelMutationsOptions {
  tasks: Task[];
  clearPlanState: (taskId: number) => void;
  onTaskActionError?: (error: Error) => void;
  onDeleteSuccess?: (taskId: number) => void;
  onDeleteError?: (taskId: number, error: Error) => void;
}

export function useTaskPanelMutations(options: UseTaskPanelMutationsOptions) {
  const { tasks, clearPlanState, onTaskActionError, onDeleteSuccess, onDeleteError } = options;

  const invalidateEngagementForTask = useCallback(
    async (taskId: number) => {
      const task = tasks.find((candidate) => candidate.id === taskId);
      if (!task?.engagement_id) {
        return;
      }
      await invalidateEngagementKnowledgeQueries(queryClient, task.engagement_id);
    },
    [tasks],
  );

  const taskActionMutation = useMutation({
    mutationFn: async ({ taskId, action }: { taskId: number; action: string }) => {
      const response = (await apiRequest("POST", `/api/tasks/${taskId}/${action}`)) as Response;
      if (!response.ok) {
        throw await responseToError(response, `Task ${action} failed`);
      }
    },
    onSuccess: async (_data, { taskId }) => {
      queryClient.invalidateQueries({ queryKey: ["/api/tasks/"] });
      await invalidateEngagementForTask(taskId);
    },
    onError: (error: Error) => {
      onTaskActionError?.(error);
    },
  });

  const deleteTaskMutation = useMutation({
    mutationFn: async (taskId: number) => {
      const response = (await apiRequest("DELETE", `/api/tasks/${taskId}`)) as Response;
      if (!response.ok) {
        throw await responseToError(response, "Delete failed");
      }
    },
    onSuccess: async (_data, taskId) => {
      await invalidateEngagementForTask(taskId);
      queryClient.setQueryData<Task[]>(["/api/tasks/"], (current = []) =>
        current.filter((task) => task.id !== taskId),
      );
      clearTaskState(taskId);
      clearChatSession(taskId);
      clearPlanState(taskId);
      if (getActiveChatTaskId() === taskId) {
        setActiveChatTaskId(null);
      }
      queryClient.removeQueries({ queryKey: ["interrupt-state", taskId], exact: true });
      queryClient.removeQueries({ queryKey: ["reasoning", taskId], exact: true });
      queryClient.invalidateQueries({ queryKey: ["/api/tasks/"] });
      onDeleteSuccess?.(taskId);
    },
    onError: (error: Error, taskId) => {
      onDeleteError?.(taskId, error);
    },
  });

  return {
    taskActionMutation,
    deleteTaskMutation,
  };
}
