// @vitest-environment jsdom
/**
 * Frontend retry-state stream-event handling tests.
 *
 * Verifies that ``status/retry_state`` packets:
 * - dispatch a typed retry lifecycle update into ``retry-state-store``
 * - emit a ``task-retry-state`` browser event with normalized identity
 * - leave existing ``streaming_state`` / ``run_state`` / ``interrupt_state``
 *   handling untouched (covered in fullPacket suite)
 *
 * Also covers transcript resync events flagged with
 * ``transcript_resync_required``. Resync is driven by that explicit flag, not
 * by lifecycle state names.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import useMultiTaskStreamManager, {
  __resetMultiTaskStreamManagerStateForTest,
} from "@/hooks/useMultiTaskStreamManager";
import * as chatStreamStore from "@/state/chat-stream-store";
import {
  __resetRetryStateStoreForTest,
  getRetryStateForTurn,
} from "@/state/retry-state-store";

const TASK_ID = 88001;

vi.mock("@/utils/websocket-config", () => ({
  wsConfig: {
    getWebSocketUrl: vi.fn(() => "ws://example/ws?type=agent-multi"),
  },
}));

class FakeWebSocket {
  public static CONNECTING = 0;
  public static OPEN = 1;
  public static CLOSING = 2;
  public static CLOSED = 3;

  public readonly sentMessages: string[] = [];
  public readyState = FakeWebSocket.CONNECTING;
  public onopen: (() => void) | null = null;
  public onmessage: ((event: MessageEvent<string>) => void) | null = null;
  public onclose: (() => void) | null = null;
  public onerror: (() => void) | null = null;

  public send(data: string): void {
    this.sentMessages.push(data);
  }

  public close(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  public open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  public emitMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

const sockets: FakeWebSocket[] = [];

function ensureTestLocalStorage(): Storage {
  if (
    typeof window.localStorage?.getItem === "function" &&
    typeof window.localStorage?.setItem === "function" &&
    typeof window.localStorage?.removeItem === "function"
  ) {
    return window.localStorage;
  }

  const store = new Map<string, string>();
  const localStorageLike = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => {
      store.clear();
    },
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  } satisfies Storage;

  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: localStorageLike,
  });
  return localStorageLike;
}

beforeEach(() => {
  sockets.length = 0;
  ensureTestLocalStorage().setItem("access_token", "test-token");
  vi.stubGlobal(
    "WebSocket",
    class extends FakeWebSocket {
      public constructor() {
        super();
        sockets.push(this);
      }
    } as unknown as typeof WebSocket,
  );
});

afterEach(() => {
  chatStreamStore.clearTaskState(TASK_ID);
  __resetRetryStateStoreForTest();
  __resetMultiTaskStreamManagerStateForTest();
  ensureTestLocalStorage().removeItem("access_token");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function emitRetryState(
  socket: FakeWebSocket,
  sequence: number,
  metadata: Record<string, unknown>,
): void {
  socket.emitMessage({
    type: "agent_reasoning",
    taskId: TASK_ID,
    sequence,
    packet: {
      placement: { turn_index: 1, tab_index: 1 },
      obj: {
        type: "status",
        content: "retry_state",
        metadata: { task_id: TASK_ID, ...metadata },
      },
    },
  });
}

function emitCheckpointRewindState(
  socket: FakeWebSocket,
  sequence: number,
  metadata: Record<string, unknown>,
): void {
  socket.emitMessage({
    type: "agent_reasoning",
    taskId: TASK_ID,
    sequence,
    packet: {
      placement: { turn_index: 1, tab_index: 1 },
      obj: {
        type: "status",
        content: "checkpoint_rewind_state",
        metadata: { task_id: TASK_ID, ...metadata },
      },
    },
  });
}

describe("useMultiTaskStreamManager retry_state handling", () => {
  it("normalizes retry_state metadata into the retry-state store", async () => {
    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    emitRetryState(sockets[0], 50, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      checkpoint_id: "ckpt-abc",
      retry_mode: "checkpoint",
      retry_attempt: 1,
      retry_max_attempts: 2,
      graph_name: "simple_tool",
      state: "started",
      transcript_resync_required: true,
    });

    await waitFor(() => {
      const entry = getRetryStateForTurn(TASK_ID, "task-88001-turn-3");
      expect(entry).toMatchObject({
        taskId: TASK_ID,
        turnId: "task-88001-turn-3",
        workflowId: 14,
        state: "started",
        retryAttempt: 1,
        retryMaxAttempts: 2,
        inFlight: true,
      });
    });
  });

  it("dispatches a task-retry-state compatibility event with canonical identity", async () => {
    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    let eventDetail: Record<string, unknown> | null = null;
    const listener = (event: Event) => {
      eventDetail = (event as CustomEvent<Record<string, unknown>>).detail;
    };
    window.addEventListener("task-retry-state", listener);

    emitRetryState(sockets[0], 51, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      checkpoint_id: "ckpt-abc",
      retry_mode: "checkpoint",
      retry_attempt: 1,
      retry_max_attempts: 2,
      graph_name: "simple_tool",
      state: "started",
      transcript_resync_required: true,
    });

    await waitFor(() => {
      expect(eventDetail).toMatchObject({
        taskId: TASK_ID,
        turnId: "task-88001-turn-3",
        workflowId: 14,
        state: "started",
        retryAttempt: 1,
        retryMaxAttempts: 2,
        checkpointId: "ckpt-abc",
        retryMode: "checkpoint",
        graphName: "simple_tool",
        transcriptResyncRequired: true,
        sequence: 51,
      });
    });

    window.removeEventListener("task-retry-state", listener);
  });

  it("ignores retry_state events with unrecognized lifecycle states", async () => {
    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    emitRetryState(sockets[0], 60, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      state: "garbage_state_value",
    });

    // The store should not record an entry when the lifecycle state is
    // unrecognized; the bubble must keep its server-derived disabled
    // state instead of being driven by client-side guesses.
    expect(getRetryStateForTurn(TASK_ID, "task-88001-turn-3")).toBeNull();
  });

  it("triggers resetTaskStreamForResync when transcript_resync_required is set", async () => {
    const resyncSpy = vi.spyOn(chatStreamStore, "resetTaskStreamForResync");

    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    emitRetryState(sockets[0], 70, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      state: "started",
      transcript_resync_required: true,
    });

    await waitFor(() => {
      expect(resyncSpy).toHaveBeenCalledWith(TASK_ID, 70);
    });
  });

  it("resyncs waiting_for_human retry events without clearing in-flight state", async () => {
    const resyncSpy = vi.spyOn(chatStreamStore, "resetTaskStreamForResync");

    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    emitRetryState(sockets[0], 71, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      state: "waiting_for_human",
      retry_attempt: 1,
      retry_max_attempts: 2,
      transcript_resync_required: true,
    });

    await waitFor(() => {
      const entry = getRetryStateForTurn(TASK_ID, "task-88001-turn-3");
      expect(entry).toMatchObject({
        state: "waiting_for_human",
        inFlight: true,
        retryAttempt: 1,
        retryMaxAttempts: 2,
      });
      expect(resyncSpy).toHaveBeenCalledWith(TASK_ID, 71);
    });
  });

  it("does not resync from lifecycle state alone", async () => {
    const resyncSpy = vi.spyOn(chatStreamStore, "resetTaskStreamForResync");

    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    emitRetryState(sockets[0], 72, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      state: "started",
    });

    expect(resyncSpy).not.toHaveBeenCalled();
  });

  it("handles checkpoint_rewind_state retry events through the retry store and resync path", async () => {
    const resyncSpy = vi.spyOn(chatStreamStore, "resetTaskStreamForResync");
    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    let rewindEventDetail: Record<string, unknown> | null = null;
    const listener = (event: Event) => {
      rewindEventDetail = (event as CustomEvent<Record<string, unknown>>)
        .detail;
    };
    window.addEventListener("task-checkpoint-rewind-state", listener);

    emitCheckpointRewindState(sockets[0], 73, {
      operation_kind: "retry",
      turn_id: "task-88001-turn-4",
      workflow_id: 19,
      checkpoint_id: "ckpt-rewind",
      retry_mode: "checkpoint",
      retry_attempt: 1,
      retry_max_attempts: 2,
      graph_name: "simple_tool",
      state: "started",
      transcript_resync_required: true,
    });

    await waitFor(() => {
      const entry = getRetryStateForTurn(TASK_ID, "task-88001-turn-4");
      expect(entry).toMatchObject({
        taskId: TASK_ID,
        turnId: "task-88001-turn-4",
        workflowId: 19,
        state: "started",
        retryAttempt: 1,
        retryMaxAttempts: 2,
        inFlight: true,
      });
      expect(resyncSpy).toHaveBeenCalledWith(TASK_ID, 73);
      expect(rewindEventDetail).toMatchObject({
        taskId: TASK_ID,
        operationKind: "retry",
        turnId: "task-88001-turn-4",
        workflowId: 19,
        state: "started",
        checkpointId: "ckpt-rewind",
        graphName: "simple_tool",
        transcriptResyncRequired: true,
        sequence: 73,
      });
    });

    window.removeEventListener("task-checkpoint-rewind-state", listener);
  });

  it("ignores duplicate retry_state events at or below the last applied resync sequence", async () => {
    const resyncSpy = vi.spyOn(chatStreamStore, "resetTaskStreamForResync");

    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    // First resync at sequence 1 (RuntimeStreamClient enforces sequence
    // monotonicity with at most +1 gap, so use small incremental
    // sequences here).
    emitRetryState(sockets[0], 1, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      state: "started",
      transcript_resync_required: true,
    });

    await waitFor(() => {
      expect(resyncSpy).toHaveBeenCalledTimes(1);
    });
    expect(resyncSpy).toHaveBeenLastCalledWith(TASK_ID, 1);

    // Replay duplicate event at sequence 1 (equal) — must be ignored.
    emitRetryState(sockets[0], 1, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      state: "started",
      transcript_resync_required: true,
    });

    expect(resyncSpy).toHaveBeenCalledTimes(1);
  });

  it("re-fires resync for a fresh retry attempt at a higher sequence", async () => {
    const resyncSpy = vi.spyOn(chatStreamStore, "resetTaskStreamForResync");

    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    emitRetryState(sockets[0], 1, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      state: "started",
      transcript_resync_required: true,
    });

    await waitFor(() => {
      expect(resyncSpy).toHaveBeenCalledTimes(1);
    });

    emitRetryState(sockets[0], 2, {
      turn_id: "task-88001-turn-3",
      workflow_id: 22,
      state: "started",
      transcript_resync_required: true,
    });

    await waitFor(() => {
      expect(resyncSpy).toHaveBeenCalledTimes(2);
    });
    expect(resyncSpy).toHaveBeenLastCalledWith(TASK_ID, 2);
  });

  it("triggers resync for terminal failed retry_state events with explicit flag", async () => {
    const resyncSpy = vi.spyOn(chatStreamStore, "resetTaskStreamForResync");

    renderHook(() =>
      useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }),
    );

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    emitRetryState(sockets[0], 91, {
      turn_id: "task-88001-turn-3",
      workflow_id: 14,
      state: "failed",
      retry_attempt: 1,
      retry_max_attempts: 2,
      transcript_resync_required: true,
    });

    await waitFor(() => {
      const entry = getRetryStateForTurn(TASK_ID, "task-88001-turn-3");
      expect(entry?.state).toBe("failed");
      expect(resyncSpy).toHaveBeenCalledWith(TASK_ID, 91);
    });
  });
});
