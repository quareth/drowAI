// @vitest-environment jsdom
/**
 * Verifies tenant-context headers on docker-log polling fallback requests.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useDockerLogs } from "@/hooks/useDockerLogs";

class ReconnectExhaustedTransport {
  private readonly config: any;

  public constructor(config: any) {
    this.config = config;
  }

  public connect(): void {
    this.config.onReconnectExhausted?.();
  }

  public disconnect(): void {
    // no-op
  }

  public getSocket(): { readyState: number } | null {
    return null;
  }
}

vi.mock("@/services/runtime_stream/MetricsStreamBus", () => ({
  metricsEventTarget: new EventTarget(),
  emitMetricsUpdate: vi.fn(),
  emitMetricsConnectionState: vi.fn(),
}));

vi.mock("@/services/runtime_stream/ChannelWebSocketTransport", () => ({
  CHANNEL_TRANSPORT_DEFAULTS: {
    baseRetryMs: 1_000,
    maxRetryMs: 10_000,
    pingIntervalMs: 30_000,
    connectionTimeoutMs: 15_000,
  },
  createChannelWebSocketTransportConfig: vi.fn((config: any) => config),
  ChannelWebSocketTransport: vi.fn(function MockedChannelWebSocketTransport(config: any) {
    return new ReconnectExhaustedTransport(config);
  }),
}));

function installStorageMock(): void {
  const store = new Map<string, string>();
  const mockStorage = {
    getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => {
      store.clear();
    },
  };
  vi.stubGlobal("localStorage", mockStorage);
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: mockStorage,
  });
}

describe("useDockerLogs tenant context", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    installStorageMock();
    localStorage.setItem("access_token", "tenant-token");
    localStorage.setItem("active_tenant_id", "701");

    vi.stubGlobal("WebSocket", {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
  });

  it("applies active tenant header during polling fallback", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ logs: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderHook(() => useDockerLogs({ taskId: 22, enabled: true }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });

    const [, requestOptions] = fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit];
    const headers = new Headers(requestOptions.headers);

    expect(headers.get("Authorization")).toBe("Bearer tenant-token");
    expect(headers.get("X-Active-Tenant-Id")).toBe("701");
  });
});
