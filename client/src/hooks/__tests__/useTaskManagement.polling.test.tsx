// @vitest-environment jsdom
/** Verifies task queries poll only while lifecycle transitions are unresolved. */

import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTaskManagement } from "@/hooks/useTaskManagement";
import type { Task } from "@/types";

const mocked = vi.hoisted(() => ({
  useQuery: vi.fn(),
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: mocked.useQuery,
}));

type RefetchIntervalResolver = (query: { state: { data?: Task[] } }) => number | false;

function queryWithStatus(status: string): { state: { data: Task[] } } {
  return { state: { data: [{ id: 1, status } as Task] } };
}

describe("useTaskManagement lifecycle polling", () => {
  beforeEach(() => {
    mocked.useQuery.mockReset();
    mocked.useQuery.mockReturnValue({ data: [], isLoading: false });
  });

  afterEach(() => {
    cleanup();
  });

  it("polls during transitional statuses and stops after they settle", () => {
    renderHook(() => useTaskManagement());

    const queryOptions = mocked.useQuery.mock.calls[0]?.[0];
    expect(queryOptions).toBeDefined();
    expect(queryOptions.refetchInterval).toBeTypeOf("function");
    const resolveInterval = queryOptions.refetchInterval as RefetchIntervalResolver;

    for (const status of ["created", "queued", "starting", "pausing", "resuming", "stopping"]) {
      expect(resolveInterval(queryWithStatus(status))).toBe(1_000);
    }
    for (const status of ["running", "paused", "stopped", "failed", "completed"]) {
      expect(resolveInterval(queryWithStatus(status))).toBe(false);
    }
  });

  it("preserves an explicit polling override", () => {
    renderHook(() => useTaskManagement({ refetchInterval: 5_000 }));

    expect(mocked.useQuery.mock.calls[0]?.[0]?.refetchInterval).toBe(5_000);
  });
});
