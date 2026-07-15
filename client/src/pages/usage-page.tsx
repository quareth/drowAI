/**
 * Dedicated Usage page.
 *
 * Responsibility: host the v1 rich LLM usage insights experience at `/usage`.
 * Owns only the task-selection state (`selectedTaskId`) and the page chrome
 * (navbar + sidebar + heading). All data fetching, filtering, and rendering
 * of overview cards / group breakdowns / timeline / per-call records is
 * delegated to `<UsageInsightsPanel />` — this page never touches the
 * insights hooks directly. The panel already handles the `taskId == null`
 * empty state, so we render the selector unconditionally and let the panel
 * decide what to show (see ownership checklist: dedicated-usage-page-only,
 * stable-naming, single-hook-family, task-scoped-backend-v1).
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Navbar } from "@/components/layout/navbar";
import { Sidebar } from "@/components/layout/sidebar";
import { UsageInsightsPanel } from "@/components/task/usage-insights/usage-insights-panel";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Task } from "@/types";

const NO_SELECTION_VALUE = "__none__";

export default function UsagePage() {
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);

  // Reuse the same task-list query key the rest of the app uses
  // (see `useTaskManagement`). No new endpoint, no parallel cache entry.
  const { data: tasks = [] } = useQuery<Task[]>({
    queryKey: ["/api/tasks/"],
  });

  const sortedTasks = useMemo(() => {
    const copy = [...tasks];
    copy.sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
    return copy;
  }, [tasks]);

  const selectValue =
    selectedTaskId != null ? String(selectedTaskId) : NO_SELECTION_VALUE;

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      <Navbar />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <div className="flex-1 p-6 overflow-auto">
          {/* Header: page title + one-line description + task selector. */}
          <div className="flex flex-col gap-4 mb-8 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <h1 className="text-3xl font-bold text-white mb-2">Usage</h1>
              <p className="text-gray-400">
                Per-task LLM usage, cache behavior, and cost breakdown.
              </p>
            </div>

            <div
              className="flex flex-col gap-1"
              data-testid="usage-page-task-selector"
            >
              <label
                htmlFor="usage-page-task-select"
                className="text-xs text-gray-400"
              >
                Task
              </label>
              <Select
                value={selectValue}
                onValueChange={(value) => {
                  if (value === NO_SELECTION_VALUE) {
                    setSelectedTaskId(null);
                    return;
                  }
                  const parsed = Number(value);
                  setSelectedTaskId(Number.isFinite(parsed) ? parsed : null);
                }}
              >
                <SelectTrigger
                  id="usage-page-task-select"
                  aria-label="Select task"
                  className="min-w-[240px] text-sm"
                >
                  <SelectValue placeholder="Select a task" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_SELECTION_VALUE} className="text-sm">
                    No task selected
                  </SelectItem>
                  {sortedTasks.map((task) => (
                    <SelectItem
                      key={task.id}
                      value={String(task.id)}
                      className="text-sm"
                    >
                      {task.name} (#{task.id})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <UsageInsightsPanel taskId={selectedTaskId} />
        </div>
      </div>
    </div>
  );
}
