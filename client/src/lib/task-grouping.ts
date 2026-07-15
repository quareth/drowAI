/**
 * Pure helpers to group and sort tasks by engagement for TaskPanel grouped view.
 * No React hooks or network calls — import only from shared Task types.
 */

import type { Task } from "@/types";

export interface EngagementGroup {
  engagementId: number | null;
  engagementName: string;
  engagementStatus?: string | null;
  tasks: Task[];
  statusSummary: string;
}

/** Priority order aligned with backend/domain/task_lifecycle.py TaskStatus (lower = higher priority). */
const STATUS_PRIORITY: Record<string, number> = {
  running: 0,
  resuming: 1,
  pausing: 2,
  paused: 3,
  starting: 4,
  queued: 5,
  created: 6,
  stopping: 7,
  stopped: 8,
  completed: 9,
  failed: 10,
  timeout: 11,
};

export const ACTIVE_TASK_STATUSES: ReadonlySet<string> = new Set([
  "created",
  "queued",
  "starting",
  "running",
  "paused",
  "resuming",
  "pausing",
]);

export function normalizeTaskPanelNameFilter(query: string): string {
  return query.trim().toLowerCase();
}

export function taskMatchesNameFilter(task: Task, normalizedFilter: string): boolean {
  if (normalizedFilter.length === 0) {
    return true;
  }
  return (
    task.name.toLowerCase().includes(normalizedFilter) ||
    (task.engagement_name ?? "").toLowerCase().includes(normalizedFilter)
  );
}

export function filterTasksByName(tasks: Task[], query: string): Task[] {
  const normalizedFilter = normalizeTaskPanelNameFilter(query);
  if (normalizedFilter.length === 0) {
    return tasks;
  }
  return tasks.filter((task) => taskMatchesNameFilter(task, normalizedFilter));
}

function statusRank(status: string): number {
  return STATUS_PRIORITY[status] ?? 99;
}

export function buildStatusSummary(tasks: Task[]): string {
  if (tasks.length === 0) {
    return "0 tasks";
  }
  const counts = new Map<string, number>();
  for (const t of tasks) {
    counts.set(t.status, (counts.get(t.status) ?? 0) + 1);
  }
  const parts: string[] = [`${tasks.length} task${tasks.length === 1 ? "" : "s"}`];
  for (const label of ["running", "paused", "completed", "failed"]) {
    const n = counts.get(label) ?? 0;
    if (n > 0) {
      parts.push(`${n} ${label}`);
    }
  }
  return parts.join(" · ");
}

export function groupTasksByEngagement(
  tasks: Task[],
  engagementStatusById?: ReadonlyMap<number, string | null>,
): EngagementGroup[] {
  const buckets = new Map<number | "ungrouped", Task[]>();
  for (const t of tasks) {
    const key = t.engagement_id == null ? "ungrouped" : t.engagement_id;
    if (!buckets.has(key)) {
      buckets.set(key, []);
    }
    buckets.get(key)!.push(t);
  }

  const groups: EngagementGroup[] = [];
  for (const [key, groupTasks] of buckets) {
    const engagementId = key === "ungrouped" ? null : key;
    const engagementName =
      engagementId === null
        ? "Ungrouped"
        : groupTasks.find((x) => x.engagement_name)?.engagement_name?.trim() ||
          `Engagement ${engagementId}`;

    const sortedTasks = [...groupTasks].sort((a, b) => {
      const pr = statusRank(a.status) - statusRank(b.status);
      if (pr !== 0) {
        return pr;
      }
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });

    groups.push({
      engagementId,
      engagementName,
      engagementStatus:
        engagementId === null ? null : (engagementStatusById?.get(engagementId) ?? null),
      tasks: sortedTasks,
      statusSummary: buildStatusSummary(sortedTasks),
    });
  }

  groups.sort((a, b) => {
    const aActive = a.tasks.some((t) => ACTIVE_TASK_STATUSES.has(t.status));
    const bActive = b.tasks.some((t) => ACTIVE_TASK_STATUSES.has(t.status));
    if (aActive !== bActive) {
      return aActive ? -1 : 1;
    }
    const aMax = Math.max(...a.tasks.map((t) => new Date(t.updated_at).getTime()));
    const bMax = Math.max(...b.tasks.map((t) => new Date(t.updated_at).getTime()));
    return bMax - aMax;
  });

  return groups;
}
