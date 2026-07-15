import { useEffect, useRef } from "react";

import type { Task } from "@/types";
import type { TaskRunStatus } from "@/hooks/useTaskRunState";

interface NotificationApi {
  title: string;
  description: string;
  variant?: "default" | "destructive";
}

interface UseTaskNotificationsOptions {
  activeTaskId: number | null;
  tasks: Task[];
  runStates: Record<number, TaskRunStatus>;
  notify: (input: NotificationApi) => void;
}

export function useTaskNotifications({
  activeTaskId,
  tasks,
  runStates,
  notify,
}: UseTaskNotificationsOptions): void {
  const previousRunStatesRef = useRef<Record<number, string>>({});

  useEffect(() => {
    const taskNameById = new Map(tasks.map((task) => [task.id, task.name]));
    for (const [taskIdRaw, state] of Object.entries(runStates)) {
      const taskId = Number(taskIdRaw);
      if (!Number.isFinite(taskId) || taskId === activeTaskId) continue;
      const prev = previousRunStatesRef.current[taskId] ?? "idle";
      if (prev !== state.state && state.state === "completed") {
        notify({
          title: "Background task completed",
          description: `${taskNameById.get(taskId) || `Task #${taskId}`} is completed.`,
        });
      }
      previousRunStatesRef.current[taskId] = state.state;
    }
  }, [activeTaskId, notify, runStates, tasks]);
}

export default useTaskNotifications;
