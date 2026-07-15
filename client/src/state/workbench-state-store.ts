/**
 * Purpose: Hold typed, global Overview workbench state for terminal dock visibility/focus.
 */
import { useSyncExternalStore } from "react";

export interface WorkbenchSnapshot {
  isTerminalCollapsed: boolean;
  terminalTaskId: number | null;
  terminalRequestNonce: number;
  version: number;
}

interface WorkbenchState {
  isTerminalCollapsed: boolean;
  terminalTaskId: number | null;
  terminalRequestNonce: number;
  version: number;
}

const listeners = new Set<() => void>();

const state: WorkbenchState = {
  isTerminalCollapsed: true,
  terminalTaskId: null,
  terminalRequestNonce: 0,
  version: 0,
};

let snapshot: WorkbenchSnapshot = { ...state };

function emit(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch (error) {
      console.error("[workbench-state-store] listener error", error);
    }
  }
}

function commit(changed: boolean): void {
  if (!changed) {
    return;
  }
  state.version += 1;
  snapshot = { ...state };
  emit();
}

export function openTerminalForTask(taskId: number): void {
  if (!Number.isFinite(taskId)) {
    return;
  }
  const normalizedTaskId = Math.floor(taskId);
  if (normalizedTaskId <= 0) {
    return;
  }
  state.terminalTaskId = normalizedTaskId;
  state.terminalRequestNonce += 1;
  state.isTerminalCollapsed = false;
  commit(true);
}

export function setTerminalCollapsed(isCollapsed: boolean): void {
  if (state.isTerminalCollapsed === isCollapsed) {
    return;
  }
  state.isTerminalCollapsed = isCollapsed;
  commit(true);
}

export function toggleTerminalCollapsed(): void {
  setTerminalCollapsed(!state.isTerminalCollapsed);
}

export function getWorkbenchStateSnapshot(): WorkbenchSnapshot {
  return snapshot;
}

export function resetWorkbenchState(): void {
  state.isTerminalCollapsed = true;
  state.terminalTaskId = null;
  state.terminalRequestNonce = 0;
  commit(true);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useWorkbenchStateSnapshot(): WorkbenchSnapshot {
  return useSyncExternalStore(subscribe, getWorkbenchStateSnapshot, getWorkbenchStateSnapshot);
}
