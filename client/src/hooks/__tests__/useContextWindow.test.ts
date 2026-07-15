// @vitest-environment jsdom
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useContextWindow } from "@/hooks/useContextWindow";
import { resetContextWindowStoreForTests } from "@/state/context-window-store";

const mocked = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetchMock,
}));

const bootstrapSnapshotFields = {
  ceiling_reached: false,
  recommended_next_action: "none",
  compression_candidate: false,
  turn_sequence: null,
  revision: -1,
  snapshot_kind: "bootstrap_estimate",
};

function measuredSnapshotFields(revision: number) {
  return {
    ceiling_reached: false,
    recommended_next_action: "none",
    compression_candidate: false,
    turn_sequence: revision,
    revision,
    snapshot_kind: "measured",
  };
}

afterEach(() => {
  cleanup();
  mocked.apiFetchMock.mockReset();
  resetContextWindowStoreForTests();
});

describe("useContextWindow", () => {
  it("hydrates context snapshot for the selected task and conversation", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task_id: 91,
        conversation_id: "conv-a",
        max_tokens: 128000,
        used_tokens: 64000,
        remaining_tokens: 64000,
        ratio: 0.5,
        ...bootstrapSnapshotFields,
      }),
    } as Response);

    const { result } = renderHook(() =>
      useContextWindow({
        taskId: 91,
        conversationId: "conv-a",
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.taskId).toBe(91);
      expect(result.current.snapshot.conversationId).toBe("conv-a");
      expect(result.current.snapshot.maxTokens).toBe(128000);
      expect(result.current.snapshot.usedTokens).toBe(64000);
      expect(result.current.snapshot.ratio).toBe(0.5);
    });

    expect(mocked.apiFetchMock).toHaveBeenCalledWith(
      "/api/tasks/91/chat/context-window?conversation_id=conv-a",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("applies stream updates from context-window-state events", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task_id: 77,
        conversation_id: "conv-live",
        max_tokens: 100,
        used_tokens: 40,
        remaining_tokens: 60,
        ratio: 0.4,
        ...bootstrapSnapshotFields,
      }),
    } as Response);

    const { result } = renderHook(() =>
      useContextWindow({
        taskId: 77,
        conversationId: "conv-live",
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.usedTokens).toBe(40);
    });

    window.dispatchEvent(
      new CustomEvent("context-window-state", {
        detail: {
          metadata: {
            task_id: 77,
            conversation_id: "conv-live",
            max_tokens: 100,
            used_tokens: 95,
            remaining_tokens: 5,
            ratio: 0.95,
            ...measuredSnapshotFields(1),
            ceiling_reached: true,
            recommended_next_action: "compress",
            compression_candidate: true,
          },
        },
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.usedTokens).toBe(95);
      expect(result.current.snapshot.ceilingReached).toBe(true);
      expect(result.current.snapshot.recommendedNextAction).toBe("compress");
      expect(result.current.snapshot.compressionCandidate).toBe(true);
    });
  });

  it("keeps a streamed measurement when delayed bootstrap hydration resolves", async () => {
    let resolveHydration: ((payload: Record<string, unknown>) => void) | undefined;
    const hydrationPayload = new Promise<Record<string, unknown>>((resolve) => {
      resolveHydration = resolve;
    });
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => hydrationPayload,
    } as Response);

    const { result } = renderHook(() =>
      useContextWindow({ taskId: 77, conversationId: "conv-delayed" }),
    );
    await waitFor(() => expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1));

    window.dispatchEvent(
      new CustomEvent("context-window-state", {
        detail: {
          metadata: {
            task_id: 77,
            conversation_id: "conv-delayed",
            max_tokens: 100,
            used_tokens: 90,
            remaining_tokens: 10,
            ratio: 0.9,
            ...measuredSnapshotFields(3),
          },
        },
      }),
    );
    await waitFor(() => expect(result.current.snapshot.usedTokens).toBe(90));

    resolveHydration?.({
      task_id: 77,
      conversation_id: "conv-delayed",
      max_tokens: 100,
      used_tokens: 5,
      remaining_tokens: 95,
      ratio: 0.05,
      ...bootstrapSnapshotFields,
    });

    await waitFor(() => expect(result.current.isHydrating).toBe(false));
    expect(result.current.snapshot).toMatchObject({
      usedTokens: 90,
      revision: 3,
      snapshotKind: "measured",
    });
  });

  it("keeps active snapshot isolated when task/conversation selection changes", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task_id: 10,
        conversation_id: "conv-a",
        max_tokens: 1000,
        used_tokens: 100,
        remaining_tokens: 900,
        ratio: 0.1,
        ...bootstrapSnapshotFields,
      }),
    } as Response);
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task_id: 11,
        conversation_id: "conv-b",
        max_tokens: 1000,
        used_tokens: 700,
        remaining_tokens: 300,
        ratio: 0.7,
        ...bootstrapSnapshotFields,
      }),
    } as Response);

    const { result, rerender } = renderHook(
      (props: { taskId: number; conversationId: string }) =>
        useContextWindow({ taskId: props.taskId, conversationId: props.conversationId }),
      { initialProps: { taskId: 10, conversationId: "conv-a" } },
    );

    await waitFor(() => {
      expect(result.current.snapshot.taskId).toBe(10);
      expect(result.current.snapshot.conversationId).toBe("conv-a");
      expect(result.current.snapshot.usedTokens).toBe(100);
    });

    rerender({ taskId: 11, conversationId: "conv-b" });

    await waitFor(() => {
      expect(result.current.snapshot.taskId).toBe(11);
      expect(result.current.snapshot.conversationId).toBe("conv-b");
      expect(result.current.snapshot.usedTokens).toBe(700);
    });

    window.dispatchEvent(
      new CustomEvent("context-window-state", {
        detail: {
          metadata: {
            task_id: 10,
            conversation_id: "conv-a",
            max_tokens: 1000,
            used_tokens: 999,
            remaining_tokens: 1,
            ratio: 0.999,
            ...measuredSnapshotFields(1),
          },
        },
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.taskId).toBe(11);
      expect(result.current.snapshot.conversationId).toBe("conv-b");
      expect(result.current.snapshot.usedTokens).toBe(700);
    });
  });

  it("ignores delayed wrong-key status packets and remains stable through reconnect-like lag", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task_id: 77,
        conversation_id: "conv-live",
        max_tokens: 100,
        used_tokens: 40,
        remaining_tokens: 60,
        ratio: 0.4,
        ...bootstrapSnapshotFields,
      }),
    } as Response);

    const { result } = renderHook(() =>
      useContextWindow({
        taskId: 77,
        conversationId: "conv-live",
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.usedTokens).toBe(40);
    });

    // Simulate lagged packets arriving after reconnect for stale keys.
    window.dispatchEvent(
      new CustomEvent("context-window-state", {
        detail: {
          metadata: {
            task_id: 77,
            conversation_id: "conv-stale",
            max_tokens: 100,
            used_tokens: 99,
            remaining_tokens: 1,
            ratio: 0.99,
            ...measuredSnapshotFields(1),
          },
        },
      }),
    );
    window.dispatchEvent(
      new CustomEvent("context-window-state", {
        detail: {
          metadata: {
            task_id: 88,
            conversation_id: "conv-live",
            max_tokens: 100,
            used_tokens: 98,
            remaining_tokens: 2,
            ratio: 0.98,
            ...measuredSnapshotFields(1),
          },
        },
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.taskId).toBe(77);
      expect(result.current.snapshot.conversationId).toBe("conv-live");
      expect(result.current.snapshot.usedTokens).toBe(40);
    });

    // Current-key packet still updates correctly after lagged stale events.
    window.dispatchEvent(
      new CustomEvent("context-window-state", {
        detail: {
          metadata: {
            task_id: 77,
            conversation_id: "conv-live",
            max_tokens: 100,
            used_tokens: 58,
            remaining_tokens: 42,
            ratio: 0.58,
            ...measuredSnapshotFields(1),
          },
        },
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.usedTokens).toBe(58);
    });
  });

  it("refreshes a bootstrap snapshot on llm-streaming completion for the active task", async () => {
    mocked.apiFetchMock
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          task_id: 77,
          conversation_id: "conv-live",
          max_tokens: 100,
          used_tokens: 40,
          remaining_tokens: 60,
          ratio: 0.4,
          ...bootstrapSnapshotFields,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          task_id: 77,
          conversation_id: "conv-live",
          max_tokens: 100,
          used_tokens: 55,
          remaining_tokens: 45,
          ratio: 0.55,
          ...bootstrapSnapshotFields,
        }),
      } as Response);

    const { result } = renderHook(() =>
      useContextWindow({
        taskId: 77,
        conversationId: "conv-live",
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.usedTokens).toBe(40);
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(1);
    });

    window.dispatchEvent(
      new CustomEvent("llm-streaming", {
        detail: {
          taskId: 77,
          isStreaming: false,
        },
      }),
    );

    await waitFor(() => {
      expect(result.current.snapshot.usedTokens).toBe(55);
      expect(result.current.snapshot.snapshotKind).toBe("bootstrap_estimate");
      expect(result.current.snapshot.revision).toBe(-1);
      expect(mocked.apiFetchMock).toHaveBeenCalledTimes(2);
    });
  });

  it("exposes an identity-matched compaction gate from lifecycle events", async () => {
    mocked.apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task_id: 77,
        conversation_id: "conv-live",
        max_tokens: 100,
        used_tokens: 80,
        remaining_tokens: 20,
        ratio: 0.8,
        ...bootstrapSnapshotFields,
      }),
    } as Response);

    const { result } = renderHook(() =>
      useContextWindow({ taskId: 77, conversationId: "conv-live" }),
    );
    await waitFor(() => expect(result.current.snapshot.usedTokens).toBe(80));

    const dispatchLifecycle = (
      state: string,
      sequence: number,
      overrides: Record<string, unknown> = {},
    ) => {
      window.dispatchEvent(
        new CustomEvent("context-window-state", {
          detail: {
            sequence,
            metadata: {
              task_id: 77,
              conversation_id: "conv-live",
              turn_id: "turn-77",
              epoch_id: "epoch-77",
              state,
              ...overrides,
            },
          },
        }),
      );
    };

    dispatchLifecycle("compacting", 20);
    await waitFor(() => {
      expect(result.current.isCompacting).toBe(true);
      expect(result.current.compactionGate).toMatchObject({
        turnId: "turn-77",
        epochId: "epoch-77",
      });
    });

    dispatchLifecycle("completed", 21, { epoch_id: "epoch-other" });
    await waitFor(() => expect(result.current.isCompacting).toBe(true));

    dispatchLifecycle("completed", 22);
    await waitFor(() => {
      expect(result.current.isCompacting).toBe(false);
      expect(result.current.compactionGate?.terminalState).toBe("completed");
    });
  });
});
