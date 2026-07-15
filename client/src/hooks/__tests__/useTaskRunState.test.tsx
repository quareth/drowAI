// @vitest-environment jsdom
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useTaskRunState } from "@/hooks/useTaskRunState";

const mocked = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetchMock,
}));

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function createStableWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    client,
    wrapper: ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}

afterEach(() => {
  mocked.apiFetchMock.mockReset();
});

describe("useTaskRunState", () => {
  it("hydrates once from API and applies run_state stream events", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        tasks: [
          {
            task_id: 11,
            is_streaming: true,
            queued_count: 0,
            run: { state: "running", turn_id: "task-11-turn-1", cancel_requested: false },
          },
          { task_id: 12, run: { state: "idle", turn_id: null, cancel_requested: false } },
        ],
      }),
    } as Response);

    const { result } = renderHook(() => useTaskRunState([11, 12]), { wrapper });

    await waitFor(() => {
      expect(result.current[11]?.state).toBe("running");
      expect(result.current[11]?.isActiveGeneration).toBe(true);
      expect(result.current[11]?.canStop).toBe(true);
      expect(result.current[12]?.state).toBe("idle");
    });

    window.dispatchEvent(
      new CustomEvent("task-run-state", {
        detail: {
          taskId: 12,
          state: "completed",
          turnId: "task-12-turn-1",
          cancelRequested: false,
        },
      }),
    );

    await waitFor(() => {
      expect(result.current[12]?.state).toBe("completed");
      expect(result.current[12]?.turnId).toBe("task-12-turn-1");
    });
  });

  it("accepts snake_case run-state event payload fields", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        tasks: [
          {
            task_id: 21,
            is_streaming: true,
            run: { state: "running", turn_id: "task-21-turn-1", cancel_requested: false },
          },
        ],
      }),
    } as Response);

    const { result } = renderHook(() => useTaskRunState([21]), { wrapper });
    await waitFor(() => {
      expect(result.current[21]?.state).toBe("running");
    });

    window.dispatchEvent(
      new CustomEvent("task-run-state", {
        detail: {
          task_id: 21,
          run_state: "cancelled",
          turn_id: "task-21-turn-1",
          cancel_requested: true,
        },
      }),
    );

    await waitFor(() => {
      expect(result.current[21]?.state).toBe("cancelled");
      expect(result.current[21]?.cancelRequested).toBe(true);
      expect(result.current[21]?.canStop).toBe(false);
    });
  });

  it("preserves declined as a provider-neutral terminal run state", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        tasks: [
          {
            task_id: 24,
            is_streaming: false,
            run: { state: "declined", turn_id: "task-24-turn-1", cancel_requested: false },
          },
        ],
      }),
    } as Response);

    const { result } = renderHook(() => useTaskRunState([24]), { wrapper });

    await waitFor(() => {
      expect(result.current[24]?.state).toBe("declined");
      expect(result.current[24]?.isActiveGeneration).toBe(false);
      expect(result.current[24]?.canStop).toBe(false);
    });
  });

  it("keeps authoritative running lifecycle rows stoppable across stream disconnects", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        tasks: [
          {
            task_id: 22,
            is_streaming: false,
            queued_count: 0,
            run: { state: "running", turn_id: "task-22-turn-1", cancel_requested: false },
          },
        ],
      }),
    } as Response);

    const { result } = renderHook(() => useTaskRunState([22]), { wrapper });

    await waitFor(() => {
      expect(result.current[22]?.state).toBe("running");
      expect(result.current[22]?.isStreaming).toBe(false);
      expect(result.current[22]?.isActiveGeneration).toBe(true);
      expect(result.current[22]?.canStop).toBe(true);
    });
  });

  it("makes running turns stoppable when streaming starts later", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        tasks: [
          {
            task_id: 23,
            is_streaming: false,
            run: { state: "running", turn_id: "task-23-turn-1", cancel_requested: false },
          },
        ],
      }),
    } as Response);

    const harness = createStableWrapper();
    const { result } = renderHook(() => useTaskRunState([23]), { wrapper: harness.wrapper });

    await waitFor(() => {
      expect(result.current[23]?.state).toBe("running");
      expect(result.current[23]?.turnId).toBe("task-23-turn-1");
      expect(result.current[23]?.canStop).toBe(true);
    });

    window.dispatchEvent(
      new CustomEvent("llm-streaming", {
        detail: {
          taskId: 23,
          isStreaming: true,
          queuedCount: 0,
        },
      }),
    );

    await waitFor(() => {
      expect(result.current[23]?.isActiveGeneration).toBe(true);
      expect(result.current[23]?.canStop).toBe(true);
    });
  });

  it("lets fresh API terminal state replace stale running stream override", async () => {
    mocked.apiFetchMock
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          tasks: [{ task_id: 31, run: { state: "idle", turn_id: null, cancel_requested: false } }],
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          tasks: [{ task_id: 31, run: { state: "completed", turn_id: "task-31-turn-1", cancel_requested: false } }],
        }),
      } as Response);

    const harness = createStableWrapper();
    const { result } = renderHook(() => useTaskRunState([31]), { wrapper: harness.wrapper });

    await waitFor(() => {
      expect(result.current[31]?.state).toBe("idle");
    });

    window.dispatchEvent(
      new CustomEvent("task-run-state", {
        detail: {
          taskId: 31,
          state: "running",
          turnId: "task-31-turn-1",
          cancelRequested: false,
        },
      }),
    );

    await waitFor(() => {
      expect(result.current[31]?.state).toBe("running");
      expect(result.current[31]?.canStop).toBe(true);
    });

    await harness.client.refetchQueries({ queryKey: ["task-run-state-batch"] });

    await waitFor(() => {
      expect(result.current[31]?.state).toBe("completed");
      expect(result.current[31]?.turnId).toBe("task-31-turn-1");
    });
  });
});
