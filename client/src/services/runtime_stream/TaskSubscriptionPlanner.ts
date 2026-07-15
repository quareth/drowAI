/**
 * Task subscription planning utilities for runtime stream subscriptions.
 *
 * Responsibility:
 * - compute desired task subscriptions from runtime context inputs
 * - produce deterministic subscribe/unsubscribe action diffs
 * - avoid duplicate protocol actions
 */

export interface TaskSubscriptionPlannerInput {
  runningTaskIds: number[];
  activeTaskId?: number | null;
  pinnedTaskIds?: number[];
  pendingInterruptTaskIds?: number[];
  maxSubscriptions?: number;
}

export interface TaskSubscriptionAction {
  type: "subscribe" | "unsubscribe";
  taskId: number;
}

function normalizeTaskIds(taskIds: number[]): number[] {
  return Array.from(new Set(taskIds.filter((value) => Number.isFinite(value) && value > 0))).sort((a, b) => a - b);
}

function normalizeTaskIdsInInputOrder(taskIds: number[]): number[] {
  const normalized: number[] = [];
  const seen = new Set<number>();
  for (const value of taskIds) {
    if (!Number.isFinite(value) || value <= 0) {
      continue;
    }
    if (seen.has(value)) {
      continue;
    }
    seen.add(value);
    normalized.push(value);
  }
  return normalized;
}

function normalizeSubscriptionBudget(maxSubscriptions?: number): number | null {
  if (!Number.isFinite(maxSubscriptions)) {
    return null;
  }
  const budget = Math.max(0, Math.floor(maxSubscriptions as number));
  return budget;
}

export function computeDesiredTaskSubscriptions(input: TaskSubscriptionPlannerInput): number[] {
  const desired: number[] = [];
  const seen = new Set<number>();
  const addTaskId = (taskId: number): void => {
    if (seen.has(taskId)) {
      return;
    }
    seen.add(taskId);
    desired.push(taskId);
  };

  if (Number.isFinite(input.activeTaskId) && (input.activeTaskId as number) > 0) {
    addTaskId(input.activeTaskId as number);
  }
  for (const taskId of normalizeTaskIds(input.pendingInterruptTaskIds ?? [])) {
    addTaskId(taskId);
  }
  for (const taskId of normalizeTaskIds(input.runningTaskIds)) {
    addTaskId(taskId);
  }
  for (const taskId of normalizeTaskIds(input.pinnedTaskIds ?? [])) {
    addTaskId(taskId);
  }

  const budget = normalizeSubscriptionBudget(input.maxSubscriptions);
  if (budget === null) {
    return desired;
  }
  return desired.slice(0, budget);
}

export function planSubscriptionActions(
  currentSubscribedTaskIds: number[],
  desiredTaskIds: number[],
): TaskSubscriptionAction[] {
  const current = new Set(normalizeTaskIds(currentSubscribedTaskIds));
  const desiredOrdered = normalizeTaskIdsInInputOrder(desiredTaskIds);
  const desired = new Set(desiredOrdered);
  const actions: TaskSubscriptionAction[] = [];

  for (const taskId of desiredOrdered) {
    if (!current.has(taskId)) {
      actions.push({ type: "subscribe", taskId });
    }
  }
  for (const taskId of Array.from(current).sort((a, b) => a - b)) {
    if (!desired.has(taskId)) {
      actions.push({ type: "unsubscribe", taskId });
    }
  }
  return actions;
}

export function planSubscriptionActionsFromInput(
  currentSubscribedTaskIds: number[],
  input: TaskSubscriptionPlannerInput,
): TaskSubscriptionAction[] {
  return planSubscriptionActions(currentSubscribedTaskIds, computeDesiredTaskSubscriptions(input));
}

