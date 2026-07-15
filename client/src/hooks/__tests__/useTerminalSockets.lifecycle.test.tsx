// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useTerminalSockets } from "@/hooks/useTerminalSockets";

type DisconnectCall = [number | undefined, string | undefined];

class MockChannelWebSocketTransport {
  public readonly disconnectCalls: DisconnectCall[] = [];
  public readonly sentPayloads: string[] = [];
  private readonly config: any;
  private readonly socket = {
    readyState: 0,
    binaryType: "blob" as BinaryType,
    send: (payload: string) => {
      this.sentPayloads.push(payload);
    },
  };

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
  }

  public getSocket(): { readyState: number; binaryType: BinaryType } {
    return this.socket;
  }

  public emitMessage(data: unknown): void {
    this.config.onMessage?.({ data });
  }

  public closeFromServer(code = 1006): void {
    this.socket.readyState = 3;
    this.config.onClose?.({ code });
  }
}

const mocked = vi.hoisted(() => ({
  instances: [] as MockChannelWebSocketTransport[],
}));

vi.mock("@/services/runtime_stream/ChannelWebSocketTransport", () => ({
  TERMINAL_CHANNEL_TRANSPORT_DEFAULTS: {
    baseRetryMs: 1_000,
    maxRetryMs: 30_000,
    pingIntervalMs: 25_000,
    connectionTimeoutMs: 15_000,
  },
  createChannelWebSocketTransportConfig: vi.fn((config: any) => ({
    tokenProvider: () => "token",
    websocketFactory: () => ({}),
    random: () => 0,
    ...config,
    baseRetryMs: config.runtimeDefaults?.baseRetryMs ?? 1_000,
    maxRetryMs: config.runtimeDefaults?.maxRetryMs ?? 30_000,
    pingIntervalMs: config.runtimeDefaults?.pingIntervalMs ?? 25_000,
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
  sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("useTerminalSockets lifecycle", () => {
  it("disconnects with cleanup reason on unmount", async () => {
    const { result, unmount } = renderHook(() => useTerminalSockets());

    act(() => {
      result.current.ensureConnection("term-1", 88);
    });

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(1);
    });

    unmount();

    expect(mocked.instances[0].disconnectCalls).toContainEqual([1000, "terminal hook cleanup"]);
  });

  it("queues input and reconnects when the active socket is closed", async () => {
    const { result } = renderHook(() => useTerminalSockets());

    act(() => {
      result.current.ensureConnection("term-1", 88);
    });

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(1);
    });
    act(() => {
      mocked.instances[0].emitMessage(
        JSON.stringify({ type: "session_created", session_id: "sid-1", session: {} }),
      );
    });
    act(() => {
      mocked.instances[0].closeFromServer();
    });

    act(() => {
      result.current.sendInput("term-1", "echo queued\n");
    });

    await waitFor(() => {
      expect(mocked.instances).toHaveLength(2);
    });
    act(() => {
      mocked.instances[1].emitMessage(
        JSON.stringify({ type: "session_created", session_id: "sid-2", session: {} }),
      );
    });

    expect(mocked.instances[1].sentPayloads).toContain(
      JSON.stringify({ type: "input", data: "echo queued\n" }),
    );
  });

  it("batches small terminal input for a short flush window", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useTerminalSockets());

    act(() => {
      result.current.ensureConnection("term-1", 88);
    });
    expect(mocked.instances).toHaveLength(1);
    act(() => {
      mocked.instances[0].emitMessage(
        JSON.stringify({ type: "session_created", session_id: "sid-1", session: {} }),
      );
      mocked.instances[0].sentPayloads.length = 0;
      result.current.sendInput("term-1", "a");
      result.current.sendInput("term-1", "b");
    });

    expect(mocked.instances[0].sentPayloads).toEqual([]);

    act(() => {
      vi.advanceTimersByTime(10);
    });

    expect(mocked.instances[0].sentPayloads).toEqual([
      JSON.stringify({ type: "input", data: "ab" }),
    ]);
  });

  it("flushes terminal input immediately on enter", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useTerminalSockets());

    act(() => {
      result.current.ensureConnection("term-1", 88);
    });
    expect(mocked.instances).toHaveLength(1);
    act(() => {
      mocked.instances[0].emitMessage(
        JSON.stringify({ type: "session_created", session_id: "sid-1", session: {} }),
      );
      mocked.instances[0].sentPayloads.length = 0;
      result.current.sendInput("term-1", "e");
      result.current.sendInput("term-1", "\n");
    });

    expect(mocked.instances[0].sentPayloads).toEqual([
      JSON.stringify({ type: "input", data: "e\n" }),
    ]);
  });

  it("flushes terminal input immediately on escape sequences", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useTerminalSockets());

    act(() => {
      result.current.ensureConnection("term-1", 88);
    });
    expect(mocked.instances).toHaveLength(1);
    act(() => {
      mocked.instances[0].emitMessage(
        JSON.stringify({ type: "session_created", session_id: "sid-1", session: {} }),
      );
      mocked.instances[0].sentPayloads.length = 0;
      result.current.sendInput("term-1", "x");
      result.current.sendInput("term-1", "\x1b");
    });

    expect(mocked.instances[0].sentPayloads).toEqual([
      JSON.stringify({ type: "input", data: "x\x1b" }),
    ]);
  });

  it("does not send a nudge newline when the backend reports stream mode", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useTerminalSockets());

    act(() => {
      result.current.ensureConnection("term-1", 88);
    });
    expect(mocked.instances).toHaveLength(1);
    act(() => {
      mocked.instances[0].emitMessage(
        JSON.stringify({
          type: "session_created",
          session_id: "sid-1",
          session: { stream_mode: true },
        }),
      );
      mocked.instances[0].sentPayloads.length = 0;
    });

    act(() => {
      vi.advanceTimersByTime(600);
    });

    expect(mocked.instances[0].sentPayloads).toEqual([]);
  });

  it("closes a backend terminal session explicitly and clears persisted session id", async () => {
    const { result } = renderHook(() => useTerminalSockets());

    act(() => {
      result.current.ensureConnection("term-1", 88);
    });
    expect(mocked.instances).toHaveLength(1);
    act(() => {
      mocked.instances[0].emitMessage(
        JSON.stringify({ type: "session_created", session_id: "sid-1", session: {} }),
      );
    });
    expect(sessionStorage.getItem("termsid:88")).toBe("sid-1");

    let closePromise: Promise<boolean>;
    act(() => {
      closePromise = result.current.closeSession("term-1");
    });
    expect(mocked.instances[0].sentPayloads).toContain(
      JSON.stringify({ type: "close_session", session_id: "sid-1" }),
    );

    act(() => {
      mocked.instances[0].emitMessage(
        JSON.stringify({ type: "session_closed", session_id: "sid-1" }),
      );
    });

    await expect(closePromise!).resolves.toBe(true);
    expect(sessionStorage.getItem("termsid:88")).toBeNull();
    expect(mocked.instances[0].disconnectCalls).toContainEqual([1000, "terminal session closed"]);
  });

  it("disconnects and clears local state when explicit close ack times out", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useTerminalSockets());

    act(() => {
      result.current.ensureConnection("term-1", 88);
    });
    expect(mocked.instances).toHaveLength(1);
    act(() => {
      mocked.instances[0].emitMessage(
        JSON.stringify({ type: "session_created", session_id: "sid-1", session: {} }),
      );
    });

    let closePromise: Promise<boolean>;
    act(() => {
      closePromise = result.current.closeSession("term-1", 10);
    });
    act(() => {
      vi.advanceTimersByTime(10);
    });

    await expect(closePromise!).resolves.toBe(false);
    expect(sessionStorage.getItem("termsid:88")).toBeNull();
    expect(mocked.instances[0].disconnectCalls).toContainEqual([1000, "terminal session closed"]);
  });
});
