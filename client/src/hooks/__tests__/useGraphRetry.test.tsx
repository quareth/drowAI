// @vitest-environment jsdom
/**
 * Tests for the checkpoint retry mutation hook.
 *
 * Verifies the hook posts the canonical payload and normalizes backend error
 * responses into actionable mutation errors.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useGraphRetry } from "@/hooks/useGraphRetry";

const mocked = vi.hoisted(() => ({
  apiRequestMock: vi.fn(),
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: mocked.apiRequestMock,
}));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  mocked.apiRequestMock.mockReset();
});

describe("useGraphRetry", () => {
  it("posts checkpoint retry payload to the graph retry endpoint", async () => {
    mocked.apiRequestMock.mockResolvedValue({
      ok: true,
      json: async () => ({ status: "retrying" }),
    } as Response);

    const { result } = renderHook(() => useGraphRetry(), { wrapper });
    result.current.mutate({
      taskId: 44,
      turnId: "task-44-turn-8",
      retryMode: "checkpoint",
      graphName: "simple_tool",
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(mocked.apiRequestMock).toHaveBeenCalledWith(
      "POST",
      "/api/tasks/44/graph/retry",
      {
        turn_id: "task-44-turn-8",
        retry_mode: "checkpoint",
        graph_name: "simple_tool",
      },
    );
  });

  it("surfaces backend detail when retry request fails", async () => {
    mocked.apiRequestMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Retry already in flight" }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { result } = renderHook(() => useGraphRetry(), { wrapper });
    result.current.mutate({
      taskId: 44,
      turnId: "task-44-turn-8",
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });

    expect(result.current.error).toBeInstanceOf(Error);
    expect((result.current.error as Error).message).toBe("Retry already in flight");
  });

  // Phase 0 / Task 0.4 — pin the frontend retry state gap.
  //
  // Today the backend returns HTTP 409 with body ``{"detail": "Retry
  // already in flight..."}`` for duplicate retry-spam clicks (see Task
  // 0.2). The current hook converts every non-2xx response into a thrown
  // Error, so the UI surfaces the duplicate path as a destructive toast
  // instead of as state-sync data.
  //
  // The Phase 1 + Phase 5 contract is that:
  //   * the route returns 200 with a typed retry identity payload that
  //     carries ``already_in_flight=true`` instead of 409,
  //   * the hook surfaces that payload as ``data`` (mutation success),
  //     not as a thrown ``Error``,
  //   * the typed identity includes ``checkpoint_id``, ``retry_attempt``,
  //     ``retry_max_attempts``, and ``state`` so the UI can disable the
  //     retry button until a terminal lifecycle state arrives.
  //
  // This test simulates the upstream-fixed behavior; today's hook still
  // typechecks the payload as ``any`` and downstream consumers cannot
  // narrow on the discriminator. The hook contract change is what makes
  // a ``RetryMutationResult`` import-and-usable from
  // ``client/src/hooks/useGraphRetry.ts``.
  it(
    "treats already_in_flight retry response as typed data, not as a destructive error",
    async () => {
      mocked.apiRequestMock.mockResolvedValue({
        ok: true,
        json: async () => ({
          status: "retrying",
          task_id: 44,
          turn_id: "task-44-turn-8",
          retry_mode: "checkpoint",
          workflow_id: 14,
          checkpoint_id: "ckpt-stable-abc123",
          retry_attempt: 1,
          retry_max_attempts: 2,
          state: "started",
          already_in_flight: true,
        }),
      } as Response);

      // The Phase 5 contract requires the hook to expose a typed
      // ``RetryMutationResult`` (or equivalent named export) so callers
      // can narrow without ``as`` casts. This re-import fails today
      // because the hook has no typed payload export.
      const { RetryMutationResult } = await import("@/hooks/useGraphRetry");
      expect(RetryMutationResult).toBeTruthy();

      const { result } = renderHook(() => useGraphRetry(), { wrapper });
      result.current.mutate({
        taskId: 44,
        turnId: "task-44-turn-8",
        retryMode: "checkpoint",
      });

      await waitFor(() => {
        expect(result.current.isSuccess).toBe(true);
      });

      expect(result.current.isError).toBe(false);
      const data = result.current.data as {
        status: string;
        task_id: number;
        turn_id: string;
        retry_mode: string;
        workflow_id: number;
        checkpoint_id: string;
        retry_attempt: number;
        retry_max_attempts: number;
        state: string;
        already_in_flight: boolean;
      };
      expect(data).toBeTruthy();
      expect(data.status).toBe("retrying");
      expect(data.task_id).toBe(44);
      expect(data.turn_id).toBe("task-44-turn-8");
      expect(data.retry_mode).toBe("checkpoint");
      expect(data.workflow_id).toBe(14);
      expect(data.checkpoint_id).toBe("ckpt-stable-abc123");
      expect(data.retry_attempt).toBe(1);
      expect(data.retry_max_attempts).toBe(2);
      expect(data.state).toBe("started");
      expect(data.already_in_flight).toBe(true);
    },
  );
});
