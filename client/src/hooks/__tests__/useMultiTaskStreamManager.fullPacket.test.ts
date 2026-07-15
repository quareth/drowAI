// @vitest-environment jsdom
/** Regression coverage for full multi-task stream packet compatibility events. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import useMultiTaskStreamManager from "@/hooks/useMultiTaskStreamManager";
import * as authSession from "@/lib/auth-session";
import { ACTIVE_TENANT_CHANGED_EVENT } from "@/lib/tenant-context";
import {
  clearTaskState,
  getTaskStreamSnapshot,
  setChatReadyState,
} from "@/state/chat-stream-store";
import {
  getContextCompactionGate,
  resetContextWindowStoreForTests,
} from "@/state/context-window-store";

const TASK_ID = 77701;
const TASK_B = 77702;

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
  public readonly url: string;
  public readonly protocols: string[];
  public readyState = FakeWebSocket.CONNECTING;
  public onopen: (() => void) | null = null;
  public onmessage: ((event: MessageEvent<string>) => void) | null = null;
  public onclose: (() => void) | null = null;
  public onerror: (() => void) | null = null;

  public constructor(url?: string | URL, protocols?: string | string[]) {
    this.url = String(url ?? "");
    this.protocols = Array.isArray(protocols)
      ? protocols.map(String)
      : typeof protocols === "string"
        ? [protocols]
        : [];
  }

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
      public constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        sockets.push(this);
      }
    } as unknown as typeof WebSocket,
  );
});

afterEach(() => {
  clearTaskState(TASK_ID);
  clearTaskState(TASK_B);
  resetContextWindowStoreForTests();
  ensureTestLocalStorage().removeItem("access_token");
  vi.unstubAllGlobals();
});

describe("useMultiTaskStreamManager full packet ingestion", () => {
  it("rebinds runtime websocket with the new tenant metadata after tenant switch", async () => {
    ensureTestLocalStorage().setItem("active_tenant_id", "11");
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    await waitFor(() => {
      expect(sockets).toHaveLength(1);
    });
    expect(sockets[0].url).toContain("active_tenant_id=11");
    expect(sockets[0].protocols).toContain("tenant.11");

    act(() => {
      ensureTestLocalStorage().setItem("active_tenant_id", "29");
      window.dispatchEvent(
        new CustomEvent(ACTIVE_TENANT_CHANGED_EVENT, {
          detail: { previousTenantId: 11, nextTenantId: 29 },
        }),
      );
    });

    await waitFor(() => {
      expect(sockets.length).toBeGreaterThanOrEqual(2);
    });
    const reboundSocket = sockets[sockets.length - 1];
    expect(reboundSocket.url).toContain("active_tenant_id=29");
    expect(reboundSocket.protocols).toContain("tenant.29");
  });

  it("tracks connection lifecycle in task stream state", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    await waitFor(() => {
      const snapshot = getTaskStreamSnapshot(TASK_ID);
      expect(snapshot.isConnected).toBe(false);
      expect(snapshot.isConnecting).toBe(true);
    });

    expect(sockets).toHaveLength(1);
    sockets[0].open();

    await waitFor(() => {
      const snapshot = getTaskStreamSnapshot(TASK_ID);
      expect(snapshot.isConnected).toBe(false);
      expect(snapshot.isConnecting).toBe(true);
      expect(snapshot.connectionError).toBeNull();
    });

    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    await waitFor(() => {
      const snapshot = getTaskStreamSnapshot(TASK_ID);
      expect(snapshot.isConnected).toBe(true);
      expect(snapshot.isConnecting).toBe(false);
      expect(snapshot.connectionError).toBeNull();
    });

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_ID,
      sequence: 5,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "status",
          content: "context_window",
          metadata: {
            task_id: TASK_ID,
            conversation_id: "conv-disconnect",
            turn_id: "turn-disconnect",
            epoch_id: "epoch-disconnect",
            state: "compacting",
          },
        },
      },
    });
    await waitFor(() => {
      expect(getContextCompactionGate(TASK_ID, "conv-disconnect")?.active).toBe(
        true,
      );
    });

    sockets[0].close();

    await waitFor(() => {
      const snapshot = getTaskStreamSnapshot(TASK_ID);
      expect(snapshot.isConnected).toBe(false);
      expect(snapshot.isConnecting).toBe(true);
      expect(getContextCompactionGate(TASK_ID, "conv-disconnect")).toMatchObject(
        {
          active: false,
          turnId: "turn-disconnect",
          epochId: "epoch-disconnect",
          lastSequence: 5,
          terminalState: null,
        },
      );
    });
  });

  it("forwards non-status packets into task stream store", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_ID,
      sequence: 12,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "tool_start",
          content: "tool begin",
          metadata: {
            id: "turn-1",
            ind: 1,
            turn_sequence: 1,
            step_type: "tool_start",
          },
        },
      },
    });

    await waitFor(() => {
      const snapshot = getTaskStreamSnapshot(TASK_ID);
      expect(snapshot.items.map((item) => item.type)).toContain("tool_start");
      expect(snapshot.lastSequence).toBe(12);
    });
  });

  it("emits task-run-state compatibility event for direct run_state packets", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    let eventDetail: Record<string, unknown> | null = null;
    const listener = (event: Event) => {
      eventDetail = (event as CustomEvent<Record<string, unknown>>).detail;
    };
    window.addEventListener("task-run-state", listener);

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_ID,
      sequence: 18,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "run_state",
          metadata: {
            task_id: TASK_ID,
            state: "running",
            turn_id: "turn-xyz",
            cancel_requested: false,
          },
        },
      },
    });

    await waitFor(() => {
      expect(eventDetail).toMatchObject({
        taskId: TASK_ID,
        state: "running",
        turnId: "turn-xyz",
      });
    });

    window.removeEventListener("task-run-state", listener);
  });

  it("applies chat_ready packets to chat bootstrap readiness state", async () => {
    setChatReadyState(TASK_ID, false, {
      task_id: TASK_ID,
      task_running: false,
      chat_ready: false,
      conversation_id: "default",
    });
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_ID,
      sequence: 21,
      packet: {
        type: "status",
        content: "chat_ready",
        metadata: {
          task_id: TASK_ID,
          conversation_id: "default",
          checkpointer_ready: true,
          task_running: true,
          sse_connected: true,
          chat_ready: true,
        },
      },
    });

    await waitFor(() => {
      const snapshot = getTaskStreamSnapshot(TASK_ID);
      expect(snapshot.chatReady).toBe(true);
      expect(snapshot.chatReadyMeta).toMatchObject({
        task_running: true,
        chat_ready: true,
        conversation_id: "default",
      });
    });
  });

  it("preserves the existing context-window event payload and compression diagnostics", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    let eventDetail: Record<string, unknown> | null = null;
    const listener = (event: Event) => {
      eventDetail = (event as CustomEvent<Record<string, unknown>>).detail;
    };
    window.addEventListener("context-window-state", listener);

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_ID,
      sequence: 25,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "status",
          content: "context_window",
          metadata: {
            task_id: TASK_ID,
            conversation_id: "conv-77701",
            max_tokens: 128000,
            used_tokens: 4200,
            remaining_tokens: 123800,
            ratio: 0.0328,
            ceiling_reached: false,
            recommended_next_action: "none",
            compression_candidate: false,
            compression_pass_count: 2,
            compression_tokens_before: 6400,
            compression_tokens_after: 4200,
            compression_degraded: true,
            state: "compacting",
            turn_id: "task-77701-turn-4",
            epoch_id: "77701:conv-77701:4200",
          },
        },
      },
    });

    await waitFor(() => {
      expect(eventDetail).toMatchObject({
        taskId: TASK_ID,
        sequence: 25,
      });
      expect(eventDetail?.metadata).toEqual({
        task_id: TASK_ID,
        conversation_id: "conv-77701",
        max_tokens: 128000,
        used_tokens: 4200,
        remaining_tokens: 123800,
        ratio: 0.0328,
        ceiling_reached: false,
        recommended_next_action: "none",
        compression_candidate: false,
        compression_pass_count: 2,
        compression_tokens_before: 6400,
        compression_tokens_after: 4200,
        compression_degraded: true,
        state: "compacting",
        turn_id: "task-77701-turn-4",
        epoch_id: "77701:conv-77701:4200",
      });
      expect(getContextCompactionGate(TASK_ID, "conv-77701")).toMatchObject({
        active: true,
        turnId: "task-77701-turn-4",
        epochId: "77701:conv-77701:4200",
        lastSequence: 25,
      });
    });

    window.removeEventListener("context-window-state", listener);
  });

  it("emits task-notification compatibility event for status/task_notification packets", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    let eventDetail: Record<string, unknown> | null = null;
    const listener = (event: Event) => {
      eventDetail = (event as CustomEvent<Record<string, unknown>>).detail;
    };
    window.addEventListener("task-notification", listener);

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_ID,
      sequence: 29,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "status",
          content: "task_notification",
          metadata: {
            task_id: TASK_ID,
            category: "knowledge_delta",
            title: "New task intelligence",
            body: "1 new asset",
            engagement_id: 7,
            ingestion_run_id: "run-1",
            tool_name: "nmap",
            asset_insert_count: 1,
            finding_insert_count: 0,
          },
        },
      },
    });

    await waitFor(() => {
      expect(eventDetail).toMatchObject({
        taskId: TASK_ID,
        category: "knowledge_delta",
        title: "New task intelligence",
        body: "1 new asset",
        sequence: 29,
      });
      expect((eventDetail?.metadata as Record<string, unknown>)?.engagementId).toBe(7);
      expect((eventDetail?.metadata as Record<string, unknown>)?.assetCount).toBe(1);
    });

    window.removeEventListener("task-notification", listener);
  });

  it("emits task-plan-created compatibility event for plan_created packets", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    let eventDetail: Record<string, unknown> | null = null;
    const listener = (event: Event) => {
      eventDetail = (event as CustomEvent<Record<string, unknown>>).detail;
    };
    window.addEventListener("task-plan-created", listener);

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_ID,
      sequence: 31,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "plan_created",
          goal: "Enumerate target",
          plan_steps: ["Step 1: Discover", "Step 2: Validate"],
          todo_list: [
            { id: "1", text: "Discover", status: "in_progress" },
            { id: "2", text: "Validate", status: "pending" },
          ],
          run_id: 42,
          plan_version: 3,
        },
      },
    });

    await waitFor(() => {
      expect(eventDetail).toMatchObject({
        taskId: TASK_ID,
        goal: "Enumerate target",
        runId: 42,
        planVersion: 3,
        sequence: 31,
      });
      const todoList = (eventDetail?.todoList as Array<Record<string, unknown>>) ?? [];
      expect(todoList[0]).toMatchObject({
        id: "1",
        text: "Discover",
        status: "in_progress",
      });
    });

    window.removeEventListener("task-plan-created", listener);
  });

  it("emits task-todo-progress compatibility event for todo_progress packets", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

    let eventDetail: Record<string, unknown> | null = null;
    const listener = (event: Event) => {
      eventDetail = (event as CustomEvent<Record<string, unknown>>).detail;
    };
    window.addEventListener("task-todo-progress", listener);

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: TASK_ID,
      sequence: 32,
      packet: {
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "todo_progress",
          todo_updates: [
            { id: "1", status: "completed", index: 0 },
            { id: "2", status: "in_progress", index: 1 },
          ],
          run_id: 42,
          plan_version: 3,
        },
      },
    });

    await waitFor(() => {
      expect(eventDetail).toMatchObject({
        taskId: TASK_ID,
        runId: 42,
        planVersion: 3,
        sequence: 32,
      });
      const updates = (eventDetail?.updates as Array<Record<string, unknown>>) ?? [];
      expect(updates).toHaveLength(2);
      expect(updates[0]).toMatchObject({
        id: "1",
        status: "completed",
        plan_version: 3,
      });
      expect(updates[1]).toMatchObject({
        id: "2",
        status: "in_progress",
        plan_version: 3,
      });
    });

    window.removeEventListener("task-todo-progress", listener);
  });

  it("projects mixed task states when one task is subscribed and another is rejected", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID, TASK_B], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });
    sockets[0].emitMessage({ type: "error", message: "max_subscriptions", taskId: TASK_B });

    await waitFor(() => {
      const taskASnapshot = getTaskStreamSnapshot(TASK_ID);
      const taskBSnapshot = getTaskStreamSnapshot(TASK_B);

      expect(taskASnapshot.isConnected).toBe(true);
      expect(taskASnapshot.isConnecting).toBe(false);
      expect(taskASnapshot.connectionError).toBeNull();

      expect(taskBSnapshot.isConnected).toBe(false);
      expect(taskBSnapshot.isConnecting).toBe(false);
      expect(taskBSnapshot.connectionError).toBe("max_subscriptions");
    });
  });

  it("keeps desired task connected when non-desired task overflows subscriptions", async () => {
    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });
    sockets[0].emitMessage({ type: "error", message: "max_subscriptions", taskId: TASK_B });

    await waitFor(() => {
      const desiredSnapshot = getTaskStreamSnapshot(TASK_ID);
      const nonDesiredSnapshot = getTaskStreamSnapshot(TASK_B);

      expect(desiredSnapshot.isConnected).toBe(true);
      expect(desiredSnapshot.connectionError).toBeNull();
      expect(nonDesiredSnapshot.isConnected).toBe(false);
      expect(nonDesiredSnapshot.isConnecting).toBe(false);
      expect(nonDesiredSnapshot.connectionError).toBeNull();
    });
  });

  it("routes websocket auth failures through centralized recovery and reconnects on success", async () => {
    const recoverSpy = vi
      .spyOn(authSession, "recoverSessionAfterAuthFailure")
      .mockResolvedValue(true);

    renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));

    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].emitMessage({
      type: "error",
      message: "Invalid authentication token",
      code: "token_expired",
    });

    await waitFor(() => {
      expect(recoverSpy).toHaveBeenCalledWith({
        source: "runtime_ws",
        reason: "token_expired",
      });
    });

    await waitFor(() => {
      expect(sockets.length).toBeGreaterThan(1);
    });

    recoverSpy.mockRestore();
  });
});
