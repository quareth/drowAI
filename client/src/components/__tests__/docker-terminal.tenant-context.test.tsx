// @vitest-environment jsdom
/**
 * Verifies DockerTerminal tenant-context propagation and permission-gated VPN retry control.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DockerTerminal } from "@/components/docker-terminal";

const { appendLogsMock, refreshVPNStatusMock, toastMock } = vi.hoisted(() => ({
  appendLogsMock: vi.fn(),
  refreshVPNStatusMock: vi.fn(),
  toastMock: vi.fn(),
}));

vi.mock("@/hooks/useDockerLogs", () => ({
  useDockerLogs: () => ({
    logs: [],
    isConnected: true,
    connectionType: "websocket",
    error: null,
    reconnect: vi.fn(),
    clearLogs: vi.fn(),
    appendLogs: appendLogsMock,
    containerStatus: null,
    containerStatusMessage: null,
  }),
}));

vi.mock("@/hooks/use-vpn-status", () => ({
  useVPNStatus: () => ({
    vpnStatus: null,
    refreshStatus: refreshVPNStatusMock,
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: toastMock }),
}));

vi.mock("@/hooks/use-user-timezone", () => ({
  useUserTimezone: () => "UTC",
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

describe("DockerTerminal tenant context", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    appendLogsMock.mockReset();
    refreshVPNStatusMock.mockReset();
    toastMock.mockReset();
    installStorageMock();
    localStorage.setItem("access_token", "tenant-token");
    localStorage.setItem("active_tenant_id", "701");
  });

  it("hides vpn retry for users without task control permission", () => {
    render(<DockerTerminal taskId={11} canTaskControl={false} />);

    expect(screen.queryByRole("button", { name: "VPN Retry" })).toBeNull();
  });

  it("sends active tenant header on vpn retry", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        accepted: true,
        connection_status: "reconnecting",
        logs: [{ timestamp: "now", service: "vpn", level: "info", message: "Restarting VPN" }],
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<DockerTerminal taskId={11} canTaskControl />);

    fireEvent.click(screen.getByRole("button", { name: "VPN Retry" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });

    const [, requestOptions] = fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit];
    const headers = new Headers(requestOptions.headers);

    expect(headers.get("Authorization")).toBe("Bearer tenant-token");
    expect(headers.get("X-Active-Tenant-Id")).toBe("701");
    expect(appendLogsMock).toHaveBeenCalledWith([
      { timestamp: "now", service: "vpn", level: "info", message: "Restarting VPN" },
    ]);
    expect(refreshVPNStatusMock).toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ title: "VPN reconnect initiated" }));
  });
});
