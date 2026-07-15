/**
 * Lightweight external store for the currently active chat task id.
 *
 * Purpose:
 * - expose active task selection outside chat component lifecycle
 * - keep runtime stream subscription planning stable at app-shell scope
 */
import { useSyncExternalStore } from "react";

let activeTaskId: number | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch {
      // no-op
    }
  }
}

function normalizeTaskId(value: number | null): number | null {
  if (value == null) {
    return null;
  }
  if (!Number.isFinite(value) || value <= 0) {
    return null;
  }
  return Math.floor(value);
}

export function setActiveChatTaskId(taskId: number | null): void {
  const normalized = normalizeTaskId(taskId);
  if (activeTaskId === normalized) {
    return;
  }
  activeTaskId = normalized;
  emit();
}

export function getActiveChatTaskId(): number | null {
  return activeTaskId;
}

export function useActiveChatTaskId(): number | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    () => activeTaskId,
    () => null,
  );
}

