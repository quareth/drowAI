// @vitest-environment jsdom
/**
 * Regression coverage for multiplex streaming continuity in multitask chat.
 *
 * Scenarios:
 * - task A keeps ingesting while the user view is on task B
 * - hidden/visible transitions do not require manual refresh for continuation
 * - reconnect resumes from last seen sequence and continues live ingestion
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import useMultiTaskStreamManager from "@/hooks/useMultiTaskStreamManager";
import { clearTaskState, getTaskStreamSnapshot } from "@/state/chat-stream-store";

const TASK_A = 88001;
const TASK_B = 88002;

const mocked = vi.hoisted(() => ({
  getWebSocketUrl: vi.fn(() => "ws://example/ws?type=agent-multi"),
}));

vi.mock("@/utils/websocket-config", () => ({
  wsConfig: {
    getWebSocketUrl: mocked.getWebSocketUrl,
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

beforeEach(() => {
  sockets.length = 0;
  window.localStorage.setItem("access_token", "test-token");
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
  clearTaskState(TASK_A);
  clearTaskState(TASK_B);
  window.localStorage.removeItem("access_token");
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("multitask background streaming continuity", () => {
  it("keeps task A stream continuity while user context includes task B", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_A, TASK_B], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_A,
      sequence: 1,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "tool_start",
          content: "task-a-progress",
          metadata: { id: "turn-a", ind: 1, turn_sequence: 1 },
        },
      },
    });

    await waitFor(() => {
      const taskASnapshot = getTaskStreamSnapshot(TASK_A);
      const taskBSnapshot = getTaskStreamSnapshot(TASK_B);
      expect(taskASnapshot.lastSequence).toBe(1);
      expect(taskASnapshot.items.map((item) => item.type)).toContain("tool_start");
      expect(taskBSnapshot.lastSequence).toBe(0);
    });
  });

  it("continues ingesting across hidden/visible transitions without reconnect storms", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_A], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_A,
      sequence: 2,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "tool_start",
          content: "before-visibility-toggle",
          metadata: { id: "turn-a", ind: 1, turn_sequence: 1 },
        },
      },
    });

    let visibilityState: DocumentVisibilityState = "hidden";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibilityState,
    });
    document.dispatchEvent(new Event("visibilitychange"));
    visibilityState = "visible";
    document.dispatchEvent(new Event("visibilitychange"));

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_A,
      sequence: 3,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "tool_start",
          content: "after-visibility-toggle",
          metadata: { id: "turn-a", ind: 2, turn_sequence: 1 },
        },
      },
    });

    await waitFor(() => {
      const snapshot = getTaskStreamSnapshot(TASK_A);
      expect(snapshot.lastSequence).toBe(3);
      expect(sockets).toHaveLength(1);
    });
  });

  it("recovers from reconnect and continues with monotonic sequence", async () => {
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(0);
    vi.useFakeTimers();
    try {
      renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_A], enabled: true }));

      expect(sockets).toHaveLength(1);
      sockets[0].open();

      sockets[0].emitMessage({
        type: "agent_reasoning",
        taskId: TASK_A,
        sequence: 10,
        packet: {
          placement: { turn_index: 1, tab_index: 1 },
          obj: {
            type: "tool_start",
            content: "before-reconnect",
            metadata: { id: "turn-a", ind: 1, turn_sequence: 1 },
          },
        },
      });

      sockets[0].close();
      await vi.advanceTimersByTimeAsync(1_100);

      expect(sockets).toHaveLength(2);
      sockets[1].open();

      expect(sockets[1].sentMessages).toContain(
        JSON.stringify({
          action: "subscribe",
          channel: "agent",
          taskId: TASK_A,
          last_seen_sequence: 10,
        }),
      );

      sockets[1].emitMessage({
        type: "agent_reasoning",
        taskId: TASK_A,
        sequence: 11,
        packet: {
          placement: { turn_index: 1, tab_index: 1 },
          obj: {
            type: "tool_start",
            content: "after-reconnect",
            metadata: { id: "turn-a", ind: 2, turn_sequence: 1 },
          },
        },
      });

      vi.useRealTimers();

      await waitFor(() => {
        const snapshot = getTaskStreamSnapshot(TASK_A);
        expect(snapshot.lastSequence).toBe(11);
        expect(snapshot.items.some((item) => item.content === "after-reconnect")).toBe(true);
      });
    } finally {
      randomSpy.mockRestore();
      vi.useRealTimers();
    }
  });
});

