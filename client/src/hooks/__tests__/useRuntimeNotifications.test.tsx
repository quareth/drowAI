// @vitest-environment jsdom
/**
 * Tests for runtime notification lifecycle boundaries.
 */
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ACTIVE_TENANT_CHANGED_EVENT } from "@/lib/tenant-context";
import { useRuntimeNotifications } from "@/hooks/useRuntimeNotifications";
import {
  addNotification,
  resetNotificationsForTest,
  useNotificationSnapshot,
} from "@/state/notification-store";

vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({
    user: { id: 1 },
  }),
}));

describe("useRuntimeNotifications", () => {
  beforeEach(() => {
    resetNotificationsForTest();
  });

  it("clears local notifications when the active tenant changes", () => {
    renderHook(() => useRuntimeNotifications());
    const snapshot = renderHook(() => useNotificationSnapshot());

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

    expect(snapshot.result.current.unreadCount).toBe(1);

    act(() => {
      window.dispatchEvent(
        new CustomEvent(ACTIVE_TENANT_CHANGED_EVENT, {
          detail: { previousTenantId: 1, nextTenantId: 2 },
        }),
      );
    });

    expect(snapshot.result.current.unreadCount).toBe(0);
    expect(snapshot.result.current.notifications).toHaveLength(0);
  });
});
