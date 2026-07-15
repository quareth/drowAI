/**
 * Verifies stream-first interrupt state behavior:
 * snapshot hydration on mount, no idle polling, reconnect reconciliation,
 * and stable dismissal keyed by interrupt_id.
 */
// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useInterruptState } from "@/hooks/useInterruptState";
import { clearTaskState, setConnectionState } from "@/state/chat-stream-store";
import type { GraphInterruptEventDetail } from "@/types/hitl";

const TASK_ID = 1714;

const mocked = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetchMock,
}));

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

let latestResult: ReturnType<typeof useInterruptState> | null = null;

function Harness({ taskId }: { taskId: number | null }) {
  latestResult = useInterruptState(taskId);
  return null;
}

function renderHarness(taskId: number | null) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  const renderResult = render(
    <QueryClientProvider client={queryClient}>
      <Harness taskId={taskId} />
    </QueryClientProvider>
  );
  return { queryClient, ...renderResult };
}

describe("useInterruptState", () => {
  beforeEach(() => {
    latestResult = null;
    mocked.apiFetchMock.mockReset();
    mocked.apiFetchMock.mockResolvedValue(
      jsonResponse(200, {
        has_interrupt: false,
        task_id: TASK_ID,
      }),
    );
    clearTaskState(TASK_ID);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    clearTaskState(TASK_ID);
  });

  it("hydrates pending interrupt from one snapshot fetch on mount", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        has_interrupt: true,
        task_id: TASK_ID,
        thread_id: `task-${TASK_ID}`,
        graph_name: "simple_tool",
        interrupt_type: "tool_approval",
        interrupt_id: "intr-1",
        payload: {
          type: "tool_approval",
          interrupt_id: "intr-1",
          tool_id: "shell.exec",
          tool_name: "Shell",
          parameters: { command: "ls" },
          description: "Run shell command",
        },
        resumable: true,
      }),
    );

    renderHarness(TASK_ID);

    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-1");
    });
    expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
    expect(mocked.apiFetchMock).toHaveBeenCalledWith(`/api/tasks/${TASK_ID}/interrupt`, {
      method: "GET",
    });
  });

  it("hydrates clarify_request payload without task collision", async () => {
    const otherTaskId = TASK_ID + 100;
    mocked.apiFetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        has_interrupt: false,
        task_id: TASK_ID,
      }),
    );

    const { queryClient, rerender } = renderHarness(TASK_ID);
    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
      expect(latestResult?.interrupt).toBeNull();
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("graph-interrupt", {
          detail: {
            taskId: otherTaskId,
            threadId: `task-${otherTaskId}`,
            interruptId: "intr-clarify-other",
            checkpointId: "cp-clarify-other",
            interruptType: "clarify_request",
            graphName: "deep_reasoning",
            payload: {
              type: "clarify_request",
              interrupt_id: "intr-clarify-other",
              questions: [
                {
                  question_id: "target",
                  input_type: "select",
                  label: "What host should I scan?",
                  options: ["10.0.0.1", "10.0.0.2"],
                  required: true,
                },
              ],
            },
          } satisfies GraphInterruptEventDetail,
        }),
      );
    });

    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });

    rerender(
      <QueryClientProvider client={queryClient}>
        <Harness taskId={otherTaskId} />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-clarify-other");
      expect(latestResult?.interrupt?.interruptType).toBe("clarify_request");
    });
  });

  it("does not poll repeatedly while disconnected/idle", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        has_interrupt: false,
        task_id: TASK_ID,
      }),
    );

    renderHarness(TASK_ID);

    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
    });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });

    expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
  });

  it("ignores repeated idle interrupt-state events when no interrupt is active", async () => {
    mocked.apiFetchMock.mockResolvedValue(
      jsonResponse(200, {
        has_interrupt: false,
        task_id: TASK_ID,
      }),
    );

    renderHarness(TASK_ID);

    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-interrupt-state", {
          detail: {
            taskId: TASK_ID,
            state: "idle",
          },
        }),
      );
      window.dispatchEvent(
        new CustomEvent("task-interrupt-state", {
          detail: {
            taskId: TASK_ID,
            state: "idle",
          },
        }),
      );
      window.dispatchEvent(
        new CustomEvent("task-interrupt-state", {
          detail: {
            taskId: TASK_ID,
            state: "none",
          },
        }),
      );
    });

    expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
  });

  it("reconciles exactly once per disconnected->connected transition", async () => {
    mocked.apiFetchMock.mockResolvedValue(
      jsonResponse(200, {
        has_interrupt: false,
        task_id: TASK_ID,
      }),
    );

    renderHarness(TASK_ID);

    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
    });

    act(() => {
      setConnectionState(TASK_ID, { isConnected: true, isConnecting: false });
    });
    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(2);
    });

    act(() => {
      setConnectionState(TASK_ID, { isConnected: true, isConnecting: false });
    });
    expect(mocked.apiFetchMock).toHaveBeenCalledTimes(2);

    act(() => {
      setConnectionState(TASK_ID, { isConnected: false, isConnecting: false });
    });
    act(() => {
      setConnectionState(TASK_ID, { isConnected: true, isConnecting: false });
    });
    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(3);
    });
  });

  it("keeps same-id replays hidden after dismiss unless local recovery explicitly reveals", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        has_interrupt: true,
        task_id: TASK_ID,
        thread_id: `task-${TASK_ID}`,
        graph_name: "simple_tool",
        interrupt_type: "tool_approval",
        interrupt_id: "intr-1",
        payload: {
          type: "tool_approval",
          interrupt_id: "intr-1",
          tool_id: "shell.exec",
          tool_name: "Shell",
          parameters: { command: "ls" },
          description: "Run shell command",
        },
        resumable: true,
      }),
    );

    renderHarness(TASK_ID);

    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-1");
    });
    const firstInterrupt = latestResult?.interrupt as GraphInterruptEventDetail;

    act(() => {
      latestResult?.setInterrupt(null);
    });
    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });

    act(() => {
      latestResult?.setInterrupt(firstInterrupt);
    });
    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });

    act(() => {
      latestResult?.setInterrupt(firstInterrupt, { allowDismissedReveal: true });
    });
    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-1");
    });

    act(() => {
      latestResult?.setInterrupt({
        ...firstInterrupt,
        interruptId: "intr-2",
        payload: {
          ...firstInterrupt.payload,
          interrupt_id: "intr-2",
        },
      });
    });
    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-2");
    });
  });

  it("keeps same-id interrupt dismissed across repeated pending state events", async () => {
    mocked.apiFetchMock.mockResolvedValue(
      jsonResponse(200, {
        has_interrupt: true,
        task_id: TASK_ID,
        thread_id: `task-${TASK_ID}`,
        graph_name: "simple_tool",
        interrupt_type: "tool_approval",
        interrupt_id: "intr-pending",
        payload: {
          type: "tool_approval",
          interrupt_id: "intr-pending",
          tool_id: "shell.exec",
          tool_name: "Shell",
          parameters: { command: "ls" },
          description: "Run shell command",
        },
        resumable: true,
      }),
    );

    renderHarness(TASK_ID);
    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-pending");
    });

    act(() => {
      latestResult?.setInterrupt(null);
    });
    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-interrupt-state", {
          detail: {
            taskId: TASK_ID,
            state: "pending",
            interruptId: "intr-pending",
            updatedAt: new Date().toISOString(),
          },
        }),
      );
    });
    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-interrupt-state", {
          detail: {
            taskId: TASK_ID,
            state: "pending",
            interruptId: "intr-pending",
            updatedAt: new Date().toISOString(),
          },
        }),
      );
    });
    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });
  });

  it("clear action only affects active task interrupt", async () => {
    const otherTaskId = TASK_ID + 200;
    mocked.apiFetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        has_interrupt: true,
        task_id: TASK_ID,
        thread_id: `task-${TASK_ID}`,
        graph_name: "simple_tool",
        interrupt_type: "tool_approval",
        interrupt_id: "intr-active",
        payload: {
          type: "tool_approval",
          interrupt_id: "intr-active",
          tool_id: "shell.exec",
          tool_name: "Shell",
          parameters: { command: "ls" },
          description: "Run shell command",
        },
        resumable: true,
      }),
    );

    const { queryClient, rerender } = renderHarness(TASK_ID);
    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-active");
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("graph-interrupt", {
          detail: {
            taskId: otherTaskId,
            threadId: `task-${otherTaskId}`,
            interruptId: "intr-other",
            checkpointId: "cp-other",
            interruptType: "clarify_request",
            graphName: "deep_reasoning",
            payload: {
              type: "clarify_request",
              interrupt_id: "intr-other",
              questions: [
                {
                  question_id: "target",
                  input_type: "select",
                  label: "Target?",
                  options: ["10.0.0.1", "10.0.0.2"],
                },
              ],
            },
          } satisfies GraphInterruptEventDetail,
        }),
      );
    });

    act(() => {
      latestResult?.setInterrupt(null);
    });
    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });

    rerender(
      <QueryClientProvider client={queryClient}>
        <Harness taskId={otherTaskId} />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-other");
      expect(latestResult?.interrupt?.interruptType).toBe("clarify_request");
    });
  });

  it("forces one-shot snapshot reconcile on task switch even with fresh cache", async () => {
    const nextTaskId = TASK_ID + 1;
    mocked.apiFetchMock
      .mockResolvedValueOnce(
        jsonResponse(200, {
          has_interrupt: false,
          task_id: TASK_ID,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          has_interrupt: true,
          task_id: nextTaskId,
          thread_id: `task-${nextTaskId}`,
          graph_name: "simple_tool",
          interrupt_type: "tool_approval",
          interrupt_id: "intr-next",
          payload: {
            type: "tool_approval",
            interrupt_id: "intr-next",
            tool_id: "shell.exec",
            tool_name: "Shell",
            parameters: { command: "id" },
            description: "Run shell command",
          },
          resumable: true,
        }),
      );

    const { queryClient, rerender } = renderHarness(TASK_ID);
    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
    });

    rerender(
      <QueryClientProvider client={queryClient}>
        <Harness taskId={nextTaskId} />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-next");
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(2);
    });
  });

  it("reconciles on pageshow foreground restore without polling loops", async () => {
    mocked.apiFetchMock.mockResolvedValue(
      jsonResponse(200, {
        has_interrupt: false,
        task_id: TASK_ID,
      }),
    );

    renderHarness(TASK_ID);
    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
    });

    act(() => {
      window.dispatchEvent(new Event("pageshow"));
    });

    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(2);
    });
  });

  it("clears interrupt on task-interrupt-state resolved event", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        has_interrupt: true,
        task_id: TASK_ID,
        thread_id: `task-${TASK_ID}`,
        graph_name: "simple_tool",
        interrupt_type: "tool_approval",
        interrupt_id: "intr-clear",
        payload: {
          type: "tool_approval",
          interrupt_id: "intr-clear",
          tool_id: "shell.exec",
          tool_name: "Shell",
          parameters: { command: "pwd" },
          description: "Run shell command",
        },
        resumable: true,
      }),
    );

    renderHarness(TASK_ID);
    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-clear");
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-interrupt-state", {
          detail: {
            taskId: TASK_ID,
            state: "resolved",
            interruptId: "intr-clear",
          },
        }),
      );
    });

    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });
  });

  it("keeps optimistic interrupt through first transient no-interrupt reconcile", async () => {
    let now = 1000;
    const nowSpy = vi.spyOn(Date, "now").mockImplementation(() => now);
    mocked.apiFetchMock.mockResolvedValue(
      jsonResponse(200, {
        has_interrupt: false,
        task_id: TASK_ID,
      }),
    );

    renderHarness(TASK_ID);
    await waitFor(() => {
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("graph-interrupt", {
          detail: {
            taskId: TASK_ID,
            threadId: `task-${TASK_ID}`,
            interruptId: "intr-race",
            checkpointId: "cp-race",
            interruptType: "clarify_request",
            graphName: "deep_reasoning",
            payload: {
              type: "clarify_request",
              interrupt_id: "intr-race",
              questions: [
                {
                  question_id: "target",
                  input_type: "select",
                  label: "Target?",
                  options: ["10.0.0.1", "10.0.0.2"],
                  required: true,
                },
              ],
            },
          } satisfies GraphInterruptEventDetail,
        }),
      );
    });

    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-race");
    });

    await act(async () => {
      await latestResult?.refetch();
    });
    expect(latestResult?.interrupt?.interruptId).toBe("intr-race");

    now += 4100;
    await act(async () => {
      await latestResult?.refetch();
    });
    await waitFor(() => {
      expect(latestResult?.interrupt).toBeNull();
    });
    nowSpy.mockRestore();
  });

  it("ignores stale resolved interrupt-state events for a different interrupt id", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        has_interrupt: true,
        task_id: TASK_ID,
        thread_id: `task-${TASK_ID}`,
        graph_name: "simple_tool",
        interrupt_type: "tool_approval",
        interrupt_id: "intr-keep",
        payload: {
          type: "tool_approval",
          interrupt_id: "intr-keep",
          tool_id: "shell.exec",
          tool_name: "Shell",
          parameters: { command: "whoami" },
          description: "Run shell command",
        },
        resumable: true,
      }),
    );

    renderHarness(TASK_ID);
    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-keep");
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-interrupt-state", {
          detail: {
            taskId: TASK_ID,
            state: "resolved",
            interruptId: "intr-stale",
          },
        }),
      );
    });

    await waitFor(() => {
      expect(latestResult?.interrupt?.interruptId).toBe("intr-keep");
    });
  });
});
