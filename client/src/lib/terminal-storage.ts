/**
 * Purpose: Centralize browser-side terminal dock persistence.
 *
 * This module owns the sessionStorage keys used by the terminal dock so tab
 * restore, backend session resume, and local output replay are updated
 * consistently.
 */

export interface StoredTerminalRecord {
  id?: string;
  taskId?: number | null;
  taskName?: string;
  isActive?: boolean;
}

const TERMINAL_LIST_KEY = "term:list";

const sessionKey = (taskId: number) => `termsid:${taskId}`;
const bufferKey = (taskId: number) => `termbuf:${taskId}`;

const hasStorage = (): boolean => typeof sessionStorage !== "undefined";

const normalizeTaskId = (value: unknown): number | null => {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? Math.floor(numeric) : null;
};

export function sanitizeTerminalList(records: StoredTerminalRecord[]): StoredTerminalRecord[] {
  const byKey = new Map<string, StoredTerminalRecord>();
  for (const item of records) {
    const taskId = normalizeTaskId(item.taskId);
    const id = item.id || `terminal-${taskId ?? Date.now()}`;
    const key = taskId != null ? `task:${taskId}` : `id:${id}`;
    const next: StoredTerminalRecord = {
      id,
      taskId,
      taskName: item.taskName || (taskId != null ? `Task ${taskId}` : "Terminal"),
      isActive: Boolean(item.isActive),
    };
    const existing = byKey.get(key);
    if (!existing || (!existing.isActive && next.isActive)) {
      byKey.set(key, next);
    }
  }
  const sanitized = Array.from(byKey.values());
  if (sanitized.length > 0 && !sanitized.some((item) => item.isActive)) {
    sanitized[0] = { ...sanitized[0], isActive: true };
  }
  return sanitized;
}

export function readTerminalList(): StoredTerminalRecord[] {
  if (!hasStorage()) return [];
  try {
    const raw = sessionStorage.getItem(TERMINAL_LIST_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return sanitizeTerminalList(parsed);
  } catch {
    return [];
  }
}

export function writeTerminalList(records: StoredTerminalRecord[]): void {
  if (!hasStorage()) return;
  try {
    const sanitized = sanitizeTerminalList(records);
    if (sanitized.length === 0) {
      sessionStorage.removeItem(TERMINAL_LIST_KEY);
      return;
    }
    sessionStorage.setItem(TERMINAL_LIST_KEY, JSON.stringify(sanitized));
  } catch {
    // no-op
  }
}

export function removeTerminalFromList(terminalId: string): StoredTerminalRecord[] {
  const next = sanitizeTerminalList(readTerminalList().filter((terminal) => terminal.id !== terminalId));
  writeTerminalList(next);
  return next;
}

export function getTerminalSessionId(taskId: number): string | null {
  if (!hasStorage()) return null;
  try {
    return sessionStorage.getItem(sessionKey(taskId));
  } catch {
    return null;
  }
}

export function setTerminalSessionId(taskId: number, sessionId: string): void {
  if (!hasStorage()) return;
  try {
    sessionStorage.setItem(sessionKey(taskId), sessionId);
  } catch {
    // no-op
  }
}

export function removeTerminalSessionId(taskId: number): void {
  if (!hasStorage()) return;
  try {
    sessionStorage.removeItem(sessionKey(taskId));
  } catch {
    // no-op
  }
}

export function getTerminalBuffer(taskId: number): string {
  if (!hasStorage()) return "";
  try {
    return sessionStorage.getItem(bufferKey(taskId)) || "";
  } catch {
    return "";
  }
}

export function setTerminalBuffer(taskId: number, value: string): void {
  if (!hasStorage()) return;
  try {
    sessionStorage.setItem(bufferKey(taskId), value);
  } catch {
    // no-op
  }
}

export function removeTerminalBuffer(taskId: number): void {
  if (!hasStorage()) return;
  try {
    sessionStorage.removeItem(bufferKey(taskId));
  } catch {
    // no-op
  }
}

export function clearTaskTerminalStorage(taskId: number): void {
  removeTerminalSessionId(taskId);
  removeTerminalBuffer(taskId);
}
