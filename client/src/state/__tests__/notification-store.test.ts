// @vitest-environment jsdom
/**
 * Tests for the reusable navbar notification store.
 */
import { describe, expect, it, beforeEach } from "vitest";

import {
  addNotification,
  markAllNotificationsRead,
  markNotificationRead,
  resetNotificationsForTest,
  useNotificationSnapshot,
} from "@/state/notification-store";
import { act, renderHook } from "@testing-library/react";

describe("notification-store", () => {
  beforeEach(() => {
    resetNotificationsForTest();
  });

  it("adds unread notifications and removes one when marked read", () => {
    const { result } = renderHook(() => useNotificationSnapshot());

    act(() => {
      addNotification({
        id: "n-1",
        taskId: 10,
        category: "knowledge_delta",
        title: "New task intelligence",
        body: "1 new asset",
        createdAt: "2026-01-01T00:00:00Z",
      });
    });

    expect(result.current.unreadCount).toBe(1);
    expect(result.current.notifications[0].title).toBe("New task intelligence");

    act(() => {
      markNotificationRead("n-1");
    });

    expect(result.current.unreadCount).toBe(0);
    expect(result.current.notifications).toHaveLength(0);
  });

  it("clears all notifications when marked read", () => {
    const { result } = renderHook(() => useNotificationSnapshot());

    act(() => {
      addNotification({
        id: "n-1",
        taskId: 10,
        category: "knowledge_delta",
        title: "New asset",
        body: "1 new asset",
        createdAt: "2026-01-01T00:00:00Z",
      });
    });
    act(() => {
      addNotification({
        id: "n-2",
        taskId: 11,
        category: "task",
        title: "Task event",
        body: "",
        createdAt: "2026-01-01T00:00:00Z",
      });
    });

    expect(result.current.unreadCount).toBe(2);

    act(() => {
      markAllNotificationsRead();
    });

    expect(result.current.unreadCount).toBe(0);
    expect(result.current.notifications).toHaveLength(0);
  });
});
