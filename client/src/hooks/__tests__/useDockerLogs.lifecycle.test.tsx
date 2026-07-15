// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useDockerLogs } from "@/hooks/useDockerLogs";

type DisconnectCall = [number | undefined, string | undefined];

class MockChannelWebSocketTransport {
  private readonly config: any;
  public readonly disconnectCalls: DisconnectCall[] = [];
  private readonly socket = { readyState: 0 };

  public constructor(config: any) {
    this.config = config;
  }

  public connect(): void {
    this.socket.readyState = 1;
    this.config.onOpen?.(this.socket);
  }

  public disconnect(code?: number, reason?: string): void {
    this.disconnectCalls.push([code, reason]);
    this.socket.readyState = 3;
    setTimeout(() => {
      this.config.onClose?.({
        code: code ?? 1000,
        reason: reason ?? "",
        wasClean: true,
      } as CloseEvent);
    }, 0);
  }

  public getSocket(): { readyState: number } {
    return this.socket;
  }
}

const mocked = vi.hoisted(() => ({
  instances: [] as MockChannelWebSocketTransport[],
  emitMetricsConnectionState: vi.fn(),
}));

vi.mock("@/services/runtime_stream/MetricsStreamBus", () => ({
  metricsEventTarget: new EventTarget(),
  emitMetricsUpdate: vi.fn(),
  emitMetricsConnectionState: mocked.emitMetricsConnectionState,
}));

vi.mock("@/services/runtime_stream/ChannelWebSocketTransport", () => ({
  CHANNEL_TRANSPORT_DEFAULTS: {
    baseRetryMs: 1_000,
    maxRetryMs: 10_000,
    pingIntervalMs: 30_000,
    connectionTimeoutMs: 15_000,
  },
  createChannelWebSocketTransportConfig: vi.fn((config: any) => ({
    tokenProvider: () => "token",
    websocketFactory: () => ({}),
    random: () => 0,
    ...config,
    baseRetryMs: config.runtimeDefaults?.baseRetryMs ?? 1_000,
    maxRetryMs: config.runtimeDefaults?.maxRetryMs ?? 10_000,
    pingIntervalMs: config.runtimeDefaults?.pingIntervalMs ?? 30_000,
    connectionTimeoutMs: config.runtimeDefaults?.connectionTimeoutMs ?? 15_000,
  })),
  ChannelWebSocketTransport: vi.fn(function MockedChannelWebSocketTransportConstructor(config: any) {
    const instance = new MockChannelWebSocketTransport(config);
    mocked.instances.push(instance);
    return instance;
  }),
}));

beforeEach(() => {
  mocked.instances.length = 0;
  mocked.emitMetricsConnectionState.mockClear();
  if (typeof WebSocket === "undefined") {
    vi.stubGlobal("WebSocket", {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
  }
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useDockerLogs lifecycle close reasons", () => {
  it("uses panel closed reason only when hook unmounts", async () => {
    const { unmount } = renderHook(() => useDockerLogs({ taskId: 22, enabled: true }));

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(1);
    });

    unmount();

    expect(mocked.instances[0].disconnectCalls).toContainEqual([1000, "panel closed"]);
  });

  it("uses stream disabled reason when monitor is toggled off", async () => {
    const { rerender } = renderHook(
      ({ enabled }) => useDockerLogs({ taskId: 22, enabled }),
      { initialProps: { enabled: true } },
    );

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(1);
    });

    rerender({ enabled: false });

    expect(mocked.instances[0].disconnectCalls).toContainEqual([1000, "stream disabled"]);
    expect(mocked.instances[0].disconnectCalls).not.toContainEqual([1000, "panel closed"]);
  });

  it("uses task switched reason when task id changes", async () => {
    const { rerender } = renderHook(
      ({ taskId }) => useDockerLogs({ taskId, enabled: true }),
      { initialProps: { taskId: 22 } },
    );

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(1);
    });

    rerender({ taskId: 23 });

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(2);
    });

    expect(mocked.instances[0].disconnectCalls).toContainEqual([1000, "task switched"]);
    expect(mocked.instances[0].disconnectCalls).not.toContainEqual([1000, "panel closed"]);
  });

  it("emits disconnected state during manual reconnect even if old close callback is stale", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useDockerLogs({ taskId: 22, enabled: true }));

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(1);
      expect(result.current.isConnected).toBe(true);
    });

    mocked.emitMetricsConnectionState.mockClear();

    act(() => {
      result.current.reconnect();
    });

    expect(mocked.instances[0].disconnectCalls).toContainEqual([1000, "manual reconnect"]);
    expect(mocked.emitMetricsConnectionState).toHaveBeenCalledWith({
      taskId: 22,
      state: "disconnected",
      error: null,
    });

    act(() => {
      vi.runOnlyPendingTimers();
    });
  });
});
