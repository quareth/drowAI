/** Provides the shared task-list query and lifecycle-aware refresh behavior. */

import { useQuery } from "@tanstack/react-query";
import type { Task } from "@/types";

const TRANSITIONAL_TASK_STATUSES = new Set([
  "created",
  "queued",
  "starting",
  "pausing",
  "resuming",
  "stopping",
]);
const TRANSITIONAL_TASK_REFETCH_INTERVAL_MS = 1_000;

interface TaskManagementQueryOptions {
  refetchInterval?: number | false;
}

export const useTaskManagement = (options: TaskManagementQueryOptions = {}) => {
  const { data: tasks = [], isLoading } = useQuery<Task[]>({
    queryKey: ["/api/tasks/"],
    refetchInterval:
      options.refetchInterval ??
      ((query) =>
        query.state.data?.some((task) => TRANSITIONAL_TASK_STATUSES.has(task.status))
          ? TRANSITIONAL_TASK_REFETCH_INTERVAL_MS
          : false),
    refetchIntervalInBackground: false,
  });

  return { tasks, isLoading };
};
