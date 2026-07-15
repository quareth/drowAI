import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ChannelWebSocketTransport,
  CHANNEL_TRANSPORT_DEFAULTS,
  createChannelWebSocketTransportConfig,
} from "../ChannelWebSocketTransport";

class FakeWebSocket {
  public static CONNECTING = 0;
  public static OPEN = 1;
  public static CLOSING = 2;
  public static CLOSED = 3;

  public readyState = FakeWebSocket.CONNECTING;
  public readonly sentMessages: string[] = [];
  public onopen: (() => void) | null = null;
  public onmessage: ((event: MessageEvent<string>) => void) | null = null;
  public onclose: ((event: CloseEvent) => void) | null = null;
  public onerror: ((event: Event) => void) | null = null;

  public send(data: string): void {
    this.sentMessages.push(data);
  }

  public close(eventOrCode?: Partial<CloseEvent> | number, reason?: string): void {
    this.readyState = FakeWebSocket.CLOSED;
    if (typeof eventOrCode === "number") {
      this.onclose?.({
        code: eventOrCode,
        reason: reason ?? "",
        wasClean: true,
      } as CloseEvent);
      return;
    }
    this.onclose?.((eventOrCode ?? { code: 1000, reason: "", wasClean: true }) as CloseEvent);
  }

  public open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
}

describe("ChannelWebSocketTransport", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not create a socket when token is missing", () => {
    const onMissingToken = vi.fn();
    const transport = new ChannelWebSocketTransport({
      url: "ws://example/ws?type=docker&taskId=10",
      tokenProvider: () => null,
      websocketFactory: () => new FakeWebSocket() as unknown as WebSocket,
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      onMissingToken,
    });

    transport.connect();
    expect(onMissingToken).toHaveBeenCalledTimes(1);
    expect(transport.getSocket()).toBeNull();
  });

  it("stops reconnecting after max reconnect attempts are exhausted", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const onReconnectExhausted = vi.fn();
    const transport = new ChannelWebSocketTransport({
      url: "ws://example/ws?type=docker&taskId=10",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      enableReconnect: true,
      maxReconnectAttempts: 1,
      onReconnectExhausted,
    });

    transport.connect();
    expect(sockets).toHaveLength(1);
    sockets[0].close({ code: 1006, reason: "abnormal", wasClean: false });
    vi.advanceTimersByTime(10);
    expect(sockets).toHaveLength(2);
    sockets[1].close({ code: 1006, reason: "abnormal", wasClean: false });
    vi.advanceTimersByTime(20);

    expect(onReconnectExhausted).toHaveBeenCalledTimes(1);
    expect(sockets).toHaveLength(2);
  });

  it("treats unauthorized close as terminal and does not reconnect", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const onUnauthorized = vi.fn();
    const transport = new ChannelWebSocketTransport({
      url: "ws://example/ws?type=docker&taskId=10",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      enableReconnect: true,
      onUnauthorized,
    });

    transport.connect();
    expect(sockets).toHaveLength(1);
    sockets[0].close({ code: 1008, reason: "Unauthorized", wasClean: true });
    vi.advanceTimersByTime(100);

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(sockets).toHaveLength(1);
  });

  it("sends keepalive ping payload while socket is open", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const transport = new ChannelWebSocketTransport({
      url: "ws://example/ws?type=docker&taskId=10",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      pingIntervalMs: 100,
      pingPayloadFactory: () => JSON.stringify({ type: "ping", source: "test" }),
    });

    transport.connect();
    sockets[0].open();
    vi.advanceTimersByTime(100);

    expect(sockets[0].sentMessages).toContain(JSON.stringify({ type: "ping", source: "test" }));
  });

  it("builds reusable transport config with common defaults", () => {
    const tokenProvider = vi.fn(() => "token-x");
    const websocketFactory = vi.fn(
      () => new FakeWebSocket() as unknown as WebSocket,
    );
    const config = createChannelWebSocketTransportConfig({
      url: "ws://example/ws?type=docker&taskId=11",
      runtimeDefaults: CHANNEL_TRANSPORT_DEFAULTS,
      tokenProvider,
      websocketFactory,
      random: () => 0.25,
    });

    expect(config.url).toBe("ws://example/ws?type=docker&taskId=11");
    expect(config.baseRetryMs).toBe(CHANNEL_TRANSPORT_DEFAULTS.baseRetryMs);
    expect(config.maxRetryMs).toBe(CHANNEL_TRANSPORT_DEFAULTS.maxRetryMs);
    expect(config.pingIntervalMs).toBe(CHANNEL_TRANSPORT_DEFAULTS.pingIntervalMs);
    expect(config.connectionTimeoutMs).toBe(CHANNEL_TRANSPORT_DEFAULTS.connectionTimeoutMs);
    expect(config.tokenProvider()).toBe("token-x");
    config.websocketFactory("ws://example", ["Bearer.token"]);
    expect(websocketFactory).toHaveBeenCalledWith("ws://example", ["Bearer.token"]);
  });

  it("connects with custom factory even when global WebSocket is unavailable", () => {
    const sockets: FakeWebSocket[] = [];
    const transport = new ChannelWebSocketTransport({
      url: "ws://example/ws?type=docker&taskId=12",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      pingIntervalMs: 100,
      enableReconnect: false,
    });

    const globals = globalThis as Record<string, unknown>;
    const previousWebSocket = globals.WebSocket;
    globals.WebSocket = undefined;
    try {
      transport.connect();
      expect(sockets).toHaveLength(1);
    } finally {
      globals.WebSocket = previousWebSocket;
    }
  });

  it("closes stalled handshake with explicit timeout code and reason", () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const onClose = vi.fn();
    const transport = new ChannelWebSocketTransport({
      url: "ws://example/ws?type=docker&taskId=13",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      connectionTimeoutMs: 50,
      enableReconnect: false,
      onClose,
    });

    transport.connect();
    vi.advanceTimersByTime(60);

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onClose.mock.calls[0][0]).toMatchObject({
      code: 4000,
      reason: "Connection timeout",
    });
  });

  it("propagates active tenant hint into websocket url and subprotocol metadata", () => {
    const calls: Array<{ url: string; protocols: string[] }> = [];
    const transport = new ChannelWebSocketTransport({
      url: "ws://example/ws?type=docker&taskId=14",
      tokenProvider: () => "token",
      activeTenantIdProvider: () => 23,
      websocketFactory: (url, protocols) => {
        calls.push({ url, protocols });
        return new FakeWebSocket() as unknown as WebSocket;
      },
      baseRetryMs: 10,
      maxRetryMs: 10,
      random: () => 0,
      enableReconnect: false,
    });

    transport.connect();

    expect(calls).toEqual([
      {
        url: "ws://example/ws?type=docker&taskId=14&active_tenant_id=23",
        protocols: ["Bearer.token", "tenant.23"],
      },
    ]);
  });
});
