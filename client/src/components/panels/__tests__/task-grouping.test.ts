/**
 * Tests for task grouping helpers used by TaskPanel grouped view.
 */

import { describe, expect, it } from "vitest";

import type { Task } from "@/types";
import {
  ACTIVE_TASK_STATUSES,
  filterTasksByName,
  groupTasksByEngagement,
} from "@/lib/task-grouping";

function task(overrides: Partial<Task>): Task {
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

describe("groupTasksByEngagement", () => {
  it("applies engagement status from the status map", () => {
    const groups = groupTasksByEngagement(
      [
        task({
          id: 11,
          engagement_id: 5,
          engagement_name: "Engagement Five",
          status: "running",
        }),
      ],
      new Map([[5, "archived"]]),
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].engagementStatus).toBe("archived");
  });

  it("falls back to null engagement status when map value is unknown", () => {
    const groups = groupTasksByEngagement([
      task({
        id: 12,
        engagement_id: 9,
        engagement_name: "Engagement Nine",
        status: "running",
      }),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].engagementStatus).toBeNull();
  });

  it("keeps groups with active-status tasks ahead of inactive groups", () => {
    const [activeStatus] = Array.from(ACTIVE_TASK_STATUSES);
    expect(activeStatus).toBeTruthy();

    const groups = groupTasksByEngagement([
      task({
        id: 21,
        engagement_id: 1,
        engagement_name: "Active Group",
        status: activeStatus!,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      }),
      task({
        id: 22,
        engagement_id: 2,
        engagement_name: "Inactive Group",
        status: "completed",
        created_at: "2026-01-02T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z",
      }),
    ]);

    expect(groups).toHaveLength(2);
    expect(groups[0].engagementId).toBe(1);
    expect(groups[1].engagementId).toBe(2);
  });

  it("treats newly created and queued tasks as active for visibility ordering", () => {
    expect(ACTIVE_TASK_STATUSES.has("created")).toBe(true);
    expect(ACTIVE_TASK_STATUSES.has("queued")).toBe(true);

    const groups = groupTasksByEngagement([
      task({
        id: 31,
        engagement_id: 1,
        engagement_name: "Completed Group",
        status: "completed",
        created_at: "2026-01-02T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z",
      }),
      task({
        id: 32,
        engagement_id: 2,
        engagement_name: "Created Group",
        status: "created",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      }),
      task({
        id: 33,
        engagement_id: 3,
        engagement_name: "Queued Group",
        status: "queued",
        created_at: "2026-01-03T00:00:00Z",
        updated_at: "2026-01-03T00:00:00Z",
      }),
    ]);

    expect(groups.map((group) => group.engagementId)).toEqual([3, 2, 1]);
  });
});

describe("filterTasksByName", () => {
  it("matches task names case-insensitively", () => {
    const tasks = [
      task({ id: 41, name: "FTP Validation", engagement_name: "Client Alpha" }),
      task({ id: 42, name: "DNS Sweep", engagement_name: "Client Beta" }),
    ];

    expect(filterTasksByName(tasks, "ftp").map((item) => item.id)).toEqual([41]);
  });

  it("matches engagement names when task names do not match", () => {
    const tasks = [
      task({ id: 51, name: "Credential Review", engagement_name: "Client Alpha" }),
      task({ id: 52, name: "Credential Review", engagement_name: "Client Beta" }),
    ];

    expect(filterTasksByName(tasks, "beta").map((item) => item.id)).toEqual([52]);
  });
});
