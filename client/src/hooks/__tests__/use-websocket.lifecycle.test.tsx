// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { useWebSocket } from "@/hooks/use-websocket";

type DisconnectCall = [number | undefined, string | undefined];

class MockChannelWebSocketTransport {
  public readonly disconnectCalls: DisconnectCall[] = [];
  private readonly config: any;
  private readonly socket = { readyState: 0, binaryType: "blob" as BinaryType };

  public constructor(config: any) {
    this.config = config;
  }

  public connect(): void {
    this.socket.readyState = 1;
  }

  public disconnect(code?: number, reason?: string): void {
    this.disconnectCalls.push([code, reason]);
    this.socket.readyState = 3;
  }

  public getSocket(): { readyState: number; binaryType: BinaryType } {
    return this.socket;
  }

  public emitOpen(): void {
    this.config.onOpen?.(this.socket);
  }

  public emitClose(): void {
    this.config.onClose?.({ code: 1000, reason: "normal", wasClean: true });
  }
}

const mocked = vi.hoisted(() => ({
  instances: [] as MockChannelWebSocketTransport[],
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
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useWebSocket lifecycle", () => {
  it("disconnects with hook cleanup reason on unmount", async () => {
    const { unmount } = renderHook(() =>
      useWebSocket({
        url: "/ws?type=metrics&taskId=77",
        enabled: true,
      }),
    );

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(1);
    });

    unmount();

    expect(mocked.instances[0].disconnectCalls).toContainEqual([1000, "hook cleanup"]);
  });

  it("ignores stale close callbacks from superseded transports", async () => {
    const { result, rerender } = renderHook(
      ({ url }) =>
        useWebSocket({
          url,
          enabled: true,
        }),
      {
        initialProps: { url: "/ws?type=metrics&taskId=77" },
      },
    );

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(1);
    });
    const first = mocked.instances[0];
    first.emitOpen();

    await waitFor(() => {
      expect(result.current.isConnected).toBe(true);
    });

    rerender({ url: "/ws?type=metrics&taskId=78" });
    await waitFor(() => {
      expect(mocked.instances).toHaveLength(2);
    });

    const second = mocked.instances[1];
    second.emitOpen();
    await waitFor(() => {
      expect(result.current.isConnected).toBe(true);
    });

    first.emitClose();

    await waitFor(() => {
      expect(result.current.isConnected).toBe(true);
      expect(result.current.socket).toBe(second.getSocket() as unknown as WebSocket);
    });
  });
});
