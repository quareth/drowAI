import { useQuery } from "@tanstack/react-query";
import { Task } from "@/types";

interface TaskManagementQueryOptions {
  refetchInterval?: number | false;
}

export const useTaskManagement = (options: TaskManagementQueryOptions = {}) => {
  const { data: tasks = [], isLoading } = useQuery<Task[]>({
    queryKey: ["/api/tasks/"],
    refetchInterval: options.refetchInterval,
    refetchIntervalInBackground: false,
  });

  return { tasks, isLoading };
};
