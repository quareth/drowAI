/**
 * Task-scoped presentation store for the agent-run drawer.
 *
 * Owns drawer navigation, selection, expansion, subscriptions, and bounded
 * task-local persistence without depending on streamed lifecycle data.
 */
import { useSyncExternalStore } from "react";

export const MAX_AGENT_RUN_PRESENTATION_TASK_STATES = 20;

export type AgentRunDrawerView = "list" | "detail";

export interface AgentRunPresentationState {
  isOpen: boolean;
  parentRunId: string | null;
  view: AgentRunDrawerView;
  selectedAgentRunId: string | null;
  activityExpanded: boolean;
}

export const CLOSED_AGENT_RUN_PRESENTATION_STATE: AgentRunPresentationState =
  Object.freeze({
    isOpen: false,
    parentRunId: null,
    view: "list",
    selectedAgentRunId: null,
    activityExpanded: false,
  });

const taskSnapshots = new Map<number, AgentRunPresentationState>();
const listeners = new Set<() => void>();

function isValidTaskId(taskId: number | null | undefined): taskId is number {
  return typeof taskId === "number" && Number.isFinite(taskId) && taskId > 0;
}

function emit(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch {
      // Listener failures must not break store fan-out.
    }
  }
}

function persistSnapshot(taskId: number, snapshot: AgentRunPresentationState): void {
  taskSnapshots.delete(taskId);
  taskSnapshots.set(taskId, snapshot);
  while (taskSnapshots.size > MAX_AGENT_RUN_PRESENTATION_TASK_STATES) {
    const oldestTaskId = taskSnapshots.keys().next().value;
    if (typeof oldestTaskId !== "number") {
      break;
    }
    taskSnapshots.delete(oldestTaskId);
  }
  emit();
}

export function getAgentRunPresentationSnapshot(
  taskId: number | null | undefined,
): AgentRunPresentationState {
  if (!isValidTaskId(taskId)) {
    return CLOSED_AGENT_RUN_PRESENTATION_STATE;
  }
  const snapshot = taskSnapshots.get(taskId);
  if (!snapshot) {
    return CLOSED_AGENT_RUN_PRESENTATION_STATE;
  }
  taskSnapshots.delete(taskId);
  taskSnapshots.set(taskId, snapshot);
  return snapshot;
}

export function openAgentRunList(taskId: number, parentRunId: string): void {
  if (!isValidTaskId(taskId)) {
    return;
  }
  const normalizedParentRunId = parentRunId.trim();
  if (!normalizedParentRunId) {
    return;
  }
  const current = getAgentRunPresentationSnapshot(taskId);
  const next: AgentRunPresentationState = {
    isOpen: true,
    parentRunId: normalizedParentRunId,
    view: "list",
    selectedAgentRunId: null,
    activityExpanded: false,
  };
  if (samePresentationState(current, next)) {
    return;
  }
  persistSnapshot(taskId, next);
}

export function openAgentRunDetail(
  taskId: number,
  parentRunId: string,
  agentRunId: string,
): void {
  if (!isValidTaskId(taskId)) {
    return;
  }
  const normalizedParentRunId = parentRunId.trim();
  const normalizedAgentRunId = agentRunId.trim();
  if (!normalizedParentRunId || !normalizedAgentRunId) {
    return;
  }
  const current = getAgentRunPresentationSnapshot(taskId);
  const next: AgentRunPresentationState = {
    isOpen: true,
    parentRunId: normalizedParentRunId,
    view: "detail",
    selectedAgentRunId: normalizedAgentRunId,
    activityExpanded: false,
  };
  if (samePresentationState(current, next)) {
    return;
  }
  persistSnapshot(taskId, next);
}

export function setAgentRunActivityExpanded(taskId: number, expanded: boolean): void {
  if (!isValidTaskId(taskId)) {
    return;
  }
  const current = getAgentRunPresentationSnapshot(taskId);
  if (current.activityExpanded === expanded) {
    return;
  }
  persistSnapshot(taskId, { ...current, activityExpanded: expanded });
}

export function returnAgentRunDrawerToList(taskId: number): void {
  if (!isValidTaskId(taskId)) {
    return;
  }
  const current = getAgentRunPresentationSnapshot(taskId);
  if (!current.isOpen || current.view === "list") {
    return;
  }
  persistSnapshot(taskId, {
    ...current,
    view: "list",
    selectedAgentRunId: null,
    activityExpanded: false,
  });
}

export function closeAgentRunDrawer(taskId: number): void {
  if (!isValidTaskId(taskId)) {
    return;
  }
  const current = getAgentRunPresentationSnapshot(taskId);
  if (samePresentationState(current, CLOSED_AGENT_RUN_PRESENTATION_STATE)) {
    return;
  }
  persistSnapshot(taskId, CLOSED_AGENT_RUN_PRESENTATION_STATE);
}

export function clearAgentRunPresentationForTask(taskId: number): void {
  if (!taskSnapshots.delete(taskId)) {
    return;
  }
  emit();
}

export function resetAgentRunPresentationStoreForTests(): void {
  taskSnapshots.clear();
  emit();
}

export function useAgentRunPresentationSnapshot(
  taskId: number | null | undefined,
): AgentRunPresentationState {
  return useSyncExternalStore(
    subscribe,
    () => getAgentRunPresentationSnapshot(taskId),
    () => CLOSED_AGENT_RUN_PRESENTATION_STATE,
  );
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function samePresentationState(
  a: AgentRunPresentationState,
  b: AgentRunPresentationState,
): boolean {
  return (
    a.isOpen === b.isOpen &&
    a.parentRunId === b.parentRunId &&
    a.view === b.view &&
    a.selectedAgentRunId === b.selectedAgentRunId &&
    a.activityExpanded === b.activityExpanded
  );
}
