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

    act(() => {
      window.dispatchEvent(
        new CustomEvent("llm-streaming", {
          detail: {
            taskId: 91,
            isStreaming: true,
          },
        }),
      );
    });
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

    act(() => {
      window.dispatchEvent(
        new CustomEvent("llm-streaming", {
          detail: {
            taskId: 92,
            isStreaming: true,
          },
        }),
      );
    });

    await act(async () => {
      await result.current.onUserSend("queued via run state");
    });
    expect(result.current.count).toBe(1);
    mocked.streamingState.isStreaming = false;
    rerender();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-run-state", {
          detail: {
            taskId: 92,
            state: "completed",
          },
        }),
      );
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(sendQueued).toHaveBeenCalledTimes(1);
  });
});

