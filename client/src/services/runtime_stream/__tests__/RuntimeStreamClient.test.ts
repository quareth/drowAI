import { describe, it, expect, vi, afterEach } from "vitest";

import {
  RuntimeStreamClient,
  createRuntimeStreamTransportCore,
  resolveRuntimeStreamAuthFailure,
} from "../RuntimeStreamClient";

class FakeWebSocket {
  public static CONNECTING = 0;
  public static OPEN = 1;
  public static CLOSING = 2;
  public static CLOSED = 3;

  public readyState = FakeWebSocket.CONNECTING;
  public readonly sentMessages: string[] = [];

  public onopen: (() => void) | null = null;
  public onmessage: ((event: MessageEvent<string>) => void) | null = null;
  public onclose: ((event?: CloseEvent) => void) | null = null;
  public onerror: (() => void) | null = null;

  public send(data: string): void {
    this.sentMessages.push(data);
  }

  public close(event?: Partial<CloseEvent>): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(event as CloseEvent);
  }

  public open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  public emitMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

function countSubscribeMessages(socket: FakeWebSocket, taskId: number): number {
  return socket.sentMessages.filter((message) => {
    try {
      const parsed = JSON.parse(message) as Record<string, unknown>;
      return parsed.action === "subscribe" && parsed.taskId === taskId;
    } catch {
      return false;
    }
  }).length;
}

describe("RuntimeStreamClient", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("supports an injected socket without a global WebSocket constructor", () => {
    vi.stubGlobal("WebSocket", undefined);
    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
    });

    client.setDesiredTaskIds([101]);
    client.connect();
    sockets[0].open();

    expect(countSubscribeMessages(sockets[0], 101)).toBe(1);
  });

  it("tracks pending_subscribe and moves to subscribed only on ack", () => {
    const sockets: FakeWebSocket[] = [];
    const statePhases: string[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      onSubscriptionStateChange: (_taskId, state) => {
        statePhases.push(state.phase);
      },
    });

    client.setDesiredTaskIds([101]);
    client.connect();
    expect(sockets).toHaveLength(1);
    sockets[0].open();

    expect(client.getTaskSubscriptionState(101).phase).toBe("pending_subscribe");
    sockets[0].emitMessage({ type: "subscribed", taskId: 101 });
    expect(client.getTaskSubscriptionState(101).phase).toBe("subscribed");
    expect(statePhases).toContain("pending_subscribe");
    expect(statePhases).toContain("subscribed");
  });

  it("exposes transport core factory for reuse", () => {
    vi.useFakeTimers();
    const ping = vi.fn();
    const core = createRuntimeStreamTransportCore({
      tokenProvider: () => "token-123",
      activeTenantIdProvider: () => 77,
      websocketFactory: (url, protocols) =>
        ({ url, protocols } as unknown as WebSocket),
      baseRetryMs: 1000,
      maxRetryMs: 2000,
      random: () => 0,
      pingIntervalMs: 30_000,
      connectionTimeoutMs: 15_000,
    });

    expect(core.getToken()).toBe("token-123");
    expect(core.buildSubprotocols("token-123")).toEqual(["Bearer.token-123", "tenant.77"]);
    expect(core.createSocket("wss://example/ws", "token-123")).toMatchObject({
      url: "wss://example/ws?active_tenant_id=77",
      protocols: ["Bearer.token-123", "tenant.77"],
    });
    expect(core.computeReconnectDelay(0)).toBe(1000);
    expect(core.computeReconnectDelay(8)).toBe(2000);
    expect(core.shouldTreatCloseAsUnauthorized({ code: 1008, reason: "Policy violation" } as CloseEvent)).toBe(true);
    expect(core.getConnectionTimeoutMs()).toBe(15_000);
    expect(core.getPingIntervalMs()).toBe(30_000);

    const stop = core.startPingKeepalive(ping);
    vi.advanceTimersByTime(90_000);
    expect(ping).toHaveBeenCalledTimes(3);
    stop();
  });

  it("does not override explicit tenant query params when creating socket", () => {
    const core = createRuntimeStreamTransportCore({
      tokenProvider: () => "token-abc",
      activeTenantIdProvider: () => 55,
      websocketFactory: (url, protocols) => ({ url, protocols } as unknown as WebSocket),
      baseRetryMs: 1000,
      maxRetryMs: 2000,
      random: () => 0,
    });

    expect(
      core.createSocket("wss://example/ws?type=agent-multi&active_tenant_id=99", "token-abc"),
    ).toMatchObject({
      url: "wss://example/ws?type=agent-multi&active_tenant_id=99",
      protocols: ["Bearer.token-abc", "tenant.55"],
    });
  });

  it("resolves auth failures from structured websocket error messages", () => {
    const reason = resolveRuntimeStreamAuthFailure({
      type: "error",
      message: "Invalid authentication token",
      code: "token_expired",
    });
    expect(reason).toBe("token_expired");
  });

  it("maps tenant-context policy error codes to terminal auth failure", () => {
    const reason = resolveRuntimeStreamAuthFailure({
      type: "error",
      message: "Explicit tenant selection is required.",
      code: "explicit_tenant_required",
    });
    expect(reason).toBe("unknown_auth_error");
  });

  it("stops sweep timer when token is missing on connect", () => {
    vi.useFakeTimers();
    const websocketFactory = vi.fn();
    const statuses: Array<{ phase: string; error: string | null }> = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => null,
      websocketFactory: websocketFactory as unknown as (url: string, protocols: string[]) => WebSocket,
      onConnectionStatusChange: (status) => {
        statuses.push({ phase: status.phase, error: status.error });
      },
    });

    client.setDesiredTaskIds([321]);
    client.connect();

    const internal = client as unknown as {
      shouldRun: boolean;
      sweepTimer: ReturnType<typeof setInterval> | null;
    };

    expect(websocketFactory).not.toHaveBeenCalled();
    expect(client.getConnectionStatus()).toMatchObject({
      phase: "closed",
      error: "Missing auth token",
    });
    expect(statuses[0]).toMatchObject({ phase: "connecting", error: null });
    expect(statuses[statuses.length - 1]).toMatchObject({
      phase: "closed",
      error: "Missing auth token",
    });
    expect(internal.shouldRun).toBe(false);
    expect(internal.sweepTimer).toBeNull();

    vi.advanceTimersByTime(2_000);
    expect(internal.sweepTimer).toBeNull();
  });

  it("resubscribes with task cursor after reconnect", () => {
    vi.useFakeTimers();

    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
    });

    client.setDesiredTaskIds([101]);
    client.connect();

    expect(sockets).toHaveLength(1);
    sockets[0].open();

    expect(sockets[0].sentMessages).toContain(
      JSON.stringify({
        action: "subscribe",
        channel: "agent",
        taskId: 101,
        last_seen_sequence: 0,
      }),
    );

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: 101,
      sequence: 42,
      packet: { type: "status" },
    });

    sockets[0].close();
    vi.advanceTimersByTime(10);

    expect(sockets).toHaveLength(2);
    sockets[1].open();

    expect(sockets[1].sentMessages).toContain(
      JSON.stringify({
        action: "subscribe",
        channel: "agent",
        taskId: 101,
        last_seen_sequence: 42,
      }),
    );
  });

  it("emits connection status transitions", () => {
    const statuses: Array<{ phase: string; error: string | null }> = [];
    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      onConnectionStatusChange: (status) => {
        statuses.push({ phase: status.phase, error: status.error });
      },
    });

    client.setDesiredTaskIds([55]);
    client.connect();
    sockets[0].open();
    sockets[0].close();

    expect(statuses[0]).toMatchObject({ phase: "connecting", error: null });
    expect(statuses).toContainEqual({ phase: "open", error: null });
    expect(statuses[statuses.length - 1]).toMatchObject({ phase: "connecting", error: null });

    client.disconnect();
  });

  it("treats websocket auth failure as terminal and does not reconnect", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const authFailures: string[] = [];

    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      onAuthenticationFailure: (reason) => {
        authFailures.push(reason);
      },
    });

    client.setDesiredTaskIds([66]);
    client.connect();
    sockets[0].open();
    sockets[0].emitMessage({
      type: "error",
      message: "Invalid authentication token",
      code: "token_expired",
    });

    vi.advanceTimersByTime(5_000);

    expect(authFailures).toEqual(["token_expired"]);
    expect(client.getConnectionStatus()).toMatchObject({
      phase: "closed",
      error: "Authentication expired",
    });
    expect(sockets).toHaveLength(1);
  });

  it("treats unauthorized close code as terminal when auth frame is unavailable", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const authFailures: string[] = [];

    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      onAuthenticationFailure: (reason) => {
        authFailures.push(reason);
      },
    });

    client.setDesiredTaskIds([67]);
    client.connect();
    sockets[0].open();
    sockets[0].close({ code: 1008, reason: "Unauthorized" });

    vi.advanceTimersByTime(5_000);

    expect(authFailures).toEqual(["unknown_auth_error"]);
    expect(client.getConnectionStatus()).toMatchObject({
      phase: "closed",
      error: "Authentication failed",
    });
    expect(sockets).toHaveLength(1);
  });

  it("maps forbidden_task and max_subscriptions to terminal error state without timer retry", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
    });

    client.setDesiredTaskIds([21, 22]);
    client.connect();
    sockets[0].open();

    sockets[0].emitMessage({ type: "error", message: "forbidden_task", taskId: 21 });
    sockets[0].emitMessage({ type: "error", message: "max_subscriptions", taskId: 22 });

    expect(client.getTaskSubscriptionState(21)).toMatchObject({
      phase: "error",
      errorReason: "forbidden_task",
    });
    expect(client.getTaskSubscriptionState(22)).toMatchObject({
      phase: "error",
      errorReason: "max_subscriptions",
    });

    const subscribeCount21 = countSubscribeMessages(sockets[0], 21);
    const subscribeCount22 = countSubscribeMessages(sockets[0], 22);

    vi.advanceTimersByTime(6_000);

    expect(countSubscribeMessages(sockets[0], 21)).toBe(subscribeCount21);
    expect(countSubscribeMessages(sockets[0], 22)).toBe(subscribeCount22);
  });

  it("retries subscribe_failed and returns to pending_subscribe after backoff sweep", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
    });

    client.setDesiredTaskIds([33]);
    client.connect();
    sockets[0].open();

    sockets[0].emitMessage({ type: "error", message: "subscribe_failed", taskId: 33 });
    expect(client.getTaskSubscriptionState(33)).toMatchObject({
      phase: "error",
      errorReason: "subscribe_failed",
    });

    const initialSubscribeCount = countSubscribeMessages(sockets[0], 33);
    vi.advanceTimersByTime(500);

    expect(countSubscribeMessages(sockets[0], 33)).toBeGreaterThan(initialSubscribeCount);
    expect(client.getTaskSubscriptionState(33).phase).toBe("pending_subscribe");
  });

  it("clears pending_unsubscribe on ack and keeps removed task idle", () => {
    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
    });

    client.setDesiredTaskIds([44]);
    client.connect();
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: 44 });
    expect(client.getTaskSubscriptionState(44).phase).toBe("subscribed");

    client.setDesiredTaskIds([]);
    expect(client.getTaskSubscriptionState(44)).toMatchObject({
      desired: false,
      phase: "pending_unsubscribe",
    });

    sockets[0].emitMessage({ type: "unsubscribed", taskId: 44 });
    expect(client.getTaskSubscriptionState(44)).toMatchObject({
      desired: false,
      phase: "idle",
      errorReason: null,
    });
  });

  it("falls back from pending_unsubscribe timeout to pending_subscribe when task becomes desired again", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
    });

    client.setDesiredTaskIds([77]);
    client.connect();
    sockets[0].open();
    sockets[0].emitMessage({ type: "subscribed", taskId: 77 });

    client.setDesiredTaskIds([]);
    expect(client.getTaskSubscriptionState(77).phase).toBe("pending_unsubscribe");

    client.setDesiredTaskIds([77]);
    expect(client.getTaskSubscriptionState(77)).toMatchObject({
      desired: true,
      phase: "pending_unsubscribe",
    });

    const subscribeBeforeTimeout = countSubscribeMessages(sockets[0], 77);
    vi.advanceTimersByTime(5_500);

    expect(countSubscribeMessages(sockets[0], 77)).toBeGreaterThan(subscribeBeforeTimeout);
    expect(client.getTaskSubscriptionState(77).phase).toBe("pending_subscribe");
  });

  it("suppresses forwarding gapped envelopes before recovery replay", () => {
    const sockets: FakeWebSocket[] = [];
    const forwardedSequences: number[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      onServerMessage: (message) => {
        if (message.type === "agent_reasoning") {
          forwardedSequences.push(message.sequence);
        }
      },
    });

    client.setDesiredTaskIds([88]);
    client.connect();
    sockets[0].open();

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: 88,
      sequence: 1,
      packet: { type: "status" },
    });
    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: 88,
      sequence: 4,
      packet: { type: "status" },
    });

    expect(forwardedSequences).toEqual([1]);
  });
});
