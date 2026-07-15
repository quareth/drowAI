/**
 * App notification store for navbar unread indicators.
 *
 * Responsibilities:
 * - Hold recent unread task-scoped notifications from runtime stream events.
 * - Expose a small subscription API for React components.
 * - Keep read/removal behavior local to the UI.
 */
import { useSyncExternalStore } from "react";

const MAX_NOTIFICATIONS = 20;

export interface AppNotificationInput {
  id: string;
  taskId: number;
  category: string;
  title: string;
  body: string;
  createdAt: string;
  metadata?: Record<string, unknown>;
}

export interface AppNotification extends AppNotificationInput {
  read: boolean;
}

export interface NotificationSnapshot {
  notifications: AppNotification[];
  unreadCount: number;
}

const listeners = new Set<() => void>();
let notifications: AppNotification[] = [];
let cachedSnapshot: NotificationSnapshot = buildSnapshot();

function buildSnapshot(): NotificationSnapshot {
  return {
    notifications,
    unreadCount: notifications.reduce((count, item) => count + (item.read ? 0 : 1), 0),
  };
}

function emitChange(): void {
  cachedSnapshot = buildSnapshot();
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): NotificationSnapshot {
  return cachedSnapshot;
}

export function addNotification(input: AppNotificationInput): void {
  if (!Number.isFinite(input.taskId) || input.taskId <= 0) {
    return;
  }
  if (!input.title.trim()) {
    return;
  }
  const nextItem: AppNotification = { ...input, read: false };
  const remaining = notifications.filter((item) => item.id !== input.id);
  notifications = [nextItem, ...remaining].slice(0, MAX_NOTIFICATIONS);
  emitChange();
}

export function markNotificationRead(id: string): void {
  const next = notifications.filter((item) => item.id !== id);
  if (next.length === notifications.length) {
    return;
  }
  notifications = next;
  emitChange();
}

export function markAllNotificationsRead(): void {
  clearNotifications();
}

export function clearNotifications(): void {
  if (notifications.length === 0) {
    return;
  }
  notifications = [];
  emitChange();
}

export function resetNotificationsForTest(): void {
  notifications = [];
  emitChange();
}

export function useNotificationSnapshot(): NotificationSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
