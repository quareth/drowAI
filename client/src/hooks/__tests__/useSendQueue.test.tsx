/**
 * Verifies queued prompts advance once per completed stream lifecycle.
 */
// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSendQueue } from "@/hooks/useSendQueue";

const mocked = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  streamingState: { isStreaming: false },
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetchMock,
}));

vi.mock("@/hooks/useStreamingState", () => ({
  useStreamingState: () => mocked.streamingState,
}));

function emitStreamingState(taskId: number, isStreaming: boolean) {
  act(() => {
    window.dispatchEvent(
      new CustomEvent("llm-streaming", {
        detail: { taskId, isStreaming },
      }),
    );
  });
}

function emitCompletedRun(taskId: number) {
  act(() => {
    window.dispatchEvent(
      new CustomEvent("task-run-state", {
        detail: { taskId, state: "completed" },
      }),
    );
  });
}

describe("useSendQueue stream-status behavior", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocked.streamingState.isStreaming = false;
    mocked.apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ is_streaming: false }),
    } as Response);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("queues while streaming", async () => {
    const sendImmediate = vi.fn(async () => {});
    const sendQueued = vi.fn(async () => {});
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ is_streaming: true }),
    } as Response);

    const { result, rerender } = renderHook(() =>
      useSendQueue({
        taskId: 91,
        conversationId: "conv-1",
        messages: [],
        sendImmediate,
        sendQueued,
      }),
    );
    mocked.streamingState.isStreaming = true;
    rerender();

    emitStreamingState(91, true);
    await act(async () => {
      await Promise.resolve();
    });

    await act(async () => {
      await result.current.onUserSend("queued message");
    });
    expect(result.current.count).toBe(1);
    await act(async () => {
      await Promise.resolve();
    });

    expect(sendQueued).not.toHaveBeenCalled();
  });

  it("releases queued message when task-run-state indicates completion", async () => {
    const sendImmediate = vi.fn(async () => {});
    const sendQueued = vi.fn(async () => {});

    const { result, rerender } = renderHook(() =>
      useSendQueue({
        taskId: 92,
        conversationId: "conv-2",
        messages: [],
        sendImmediate,
        sendQueued,
      }),
    );
    mocked.streamingState.isStreaming = true;
    rerender();

    emitStreamingState(92, true);

    await act(async () => {
      await result.current.onUserSend("queued via run state");
    });
    expect(result.current.count).toBe(1);
    mocked.streamingState.isStreaming = false;
    rerender();
    emitCompletedRun(92);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(sendQueued).toHaveBeenCalledTimes(1);
  });

  it("releases only one item when a completed run emits both lifecycle signals", async () => {
    const sendImmediate = vi.fn(async () => {});
    const sendQueued = vi.fn(async () => {});

    const { result, rerender } = renderHook(() =>
      useSendQueue({
        taskId: 93,
        conversationId: "conv-3",
        messages: [],
        sendImmediate,
        sendQueued,
      }),
    );
    mocked.streamingState.isStreaming = true;
    rerender();

    emitStreamingState(93, true);

    await act(async () => {
      await result.current.onUserSend("first queued message");
      await result.current.onUserSend("second queued message");
      await result.current.onUserSend("third queued message");
    });
    expect(result.current.count).toBe(3);

    mocked.streamingState.isStreaming = false;
    rerender();
    emitCompletedRun(93);
    emitStreamingState(93, false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(550);
    });
    expect(sendQueued).toHaveBeenCalledTimes(1);

    mocked.streamingState.isStreaming = true;
    rerender();
    emitStreamingState(93, true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(550);
    });
    expect(sendQueued).toHaveBeenCalledTimes(1);
    expect(result.current.count).toBe(2);

    mocked.streamingState.isStreaming = false;
    rerender();
    emitCompletedRun(93);
    emitStreamingState(93, false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(550);
    });
    expect(sendQueued).toHaveBeenCalledTimes(2);
    expect(sendQueued).toHaveBeenNthCalledWith(2, "second queued message");
    expect(result.current.count).toBe(1);
  });

  it("keeps an item queued when streaming resumes during the dispatch delay", async () => {
    const sendImmediate = vi.fn(async () => {});
    const sendQueued = vi.fn(async () => {});

    const { result, rerender } = renderHook(() =>
      useSendQueue({
        taskId: 94,
        conversationId: "conv-4",
        messages: [],
        sendImmediate,
        sendQueued,
      }),
    );
    mocked.streamingState.isStreaming = true;
    rerender();

    emitStreamingState(94, true);
    await act(async () => {
      await result.current.onUserSend("wait for the active run");
    });

    mocked.streamingState.isStreaming = false;
    rerender();
    emitCompletedRun(94);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });

    mocked.streamingState.isStreaming = true;
    rerender();
    emitStreamingState(94, true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });

    expect(sendQueued).not.toHaveBeenCalled();
    expect(result.current.count).toBe(1);

    mocked.streamingState.isStreaming = false;
    rerender();
    emitCompletedRun(94);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(550);
    });

    expect(sendQueued).toHaveBeenCalledOnce();
    expect(result.current.count).toBe(0);
  });
});
