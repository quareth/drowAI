// @vitest-environment jsdom
/**
 * Verifies grouped-engagement orchestration hook behavior for merge/filter/status safety.
 */
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useTaskPanelEngagementGroups, useTaskPanelViewState } from "@/hooks/use-task-panel";
import type { Task } from "@/types";

const mocked = vi.hoisted(() => ({
  engagementItems: [] as Array<{ id: number; name: string; status: string | null }>,
}));

vi.mock("@/hooks/use-engagement-knowledge", () => ({
  invalidateEngagementKnowledgeQueries: vi.fn(),
  useEngagements: () => ({
    data: { items: mocked.engagementItems },
    isLoading: false,
  }),
}));

function makeTask(overrides: Partial<Task>): Task {
  return {
    id: 1,
    user_id: 1,
    name: "Task",
    status: "created",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("useTaskPanelEngagementGroups", () => {
  it("merges task-derived groups with catalog-only groups", () => {
    mocked.engagementItems = [
      { id: 7, name: "Alpha", status: "active" },
      { id: 8, name: "Bravo", status: "active" },
    ];
    const filteredTasks = [
      makeTask({
        id: 10,
        engagement_id: 7,
        engagement_name: "Alpha",
        status: "running",
      }),
    ];

    const { result } = renderHook(() =>
      useTaskPanelEngagementGroups({
        filteredTasks,
        searchQuery: "",
        showArchivedEngagements: false,
      }),
    );

    expect(result.current.engagementGroups).toHaveLength(2);
    expect(result.current.engagementGroups.some((group) => group.engagementId === 7)).toBe(true);
    expect(result.current.engagementGroups.some((group) => group.engagementId === 8)).toBe(true);
  });

  it("hides archived synthetic groups when archived toggle is off", () => {
    mocked.engagementItems = [
      { id: 20, name: "Archived", status: "archived" },
      { id: 21, name: "Active", status: "active" },
    ];

    const { result } = renderHook(() =>
      useTaskPanelEngagementGroups({
        filteredTasks: [],
        searchQuery: "",
        showArchivedEngagements: false,
      }),
    );

    expect(result.current.engagementGroups.some((group) => group.engagementId === 20)).toBe(false);
    expect(result.current.engagementGroups.some((group) => group.engagementId === 21)).toBe(true);
  });

  it("preserves unknown-status safety path for task-derived groups", () => {
    mocked.engagementItems = [];
    const filteredTasks = [
      makeTask({
        id: 30,
        engagement_id: 99,
        engagement_name: "Unknown",
        status: "running",
      }),
    ];

    const { result } = renderHook(() =>
      useTaskPanelEngagementGroups({
        filteredTasks,
        searchQuery: "",
        showArchivedEngagements: false,
      }),
    );

    const unknownGroup = result.current.engagementGroups.find((group) => group.engagementId === 99);
    expect(unknownGroup).toBeTruthy();
    expect(unknownGroup?.engagementStatus).toBeNull();
  });
});

describe("useTaskPanelViewState", () => {
  it("expands newly appearing active task groups without reopening user-collapsed groups", () => {
    const initialTasks = [
      makeTask({
        id: 40,
        engagement_id: 5,
        engagement_name: "Initial",
        status: "created",
      }),
    ];

    const { result, rerender } = renderHook(
      ({ tasks }) => useTaskPanelViewState(tasks),
      { initialProps: { tasks: initialTasks } },
    );

    expect(result.current.expandedEngagements.has(5)).toBe(true);

    act(() => {
      result.current.toggleEngagementExpanded(5);
    });
    expect(result.current.expandedEngagements.has(5)).toBe(false);

    rerender({
      tasks: [
        ...initialTasks,
        makeTask({
          id: 41,
          engagement_id: 6,
          engagement_name: "New",
          status: "queued",
        }),
      ],
    });

    expect(result.current.expandedEngagements.has(5)).toBe(false);
    expect(result.current.expandedEngagements.has(6)).toBe(true);
  });
});
