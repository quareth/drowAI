// @vitest-environment jsdom
/**
 * Verifies apiFetch auth-failure behavior:
 * - 401 on protected calls delegates to centralized recovery
 * - successful recovery retries request once
 * - auth login/register endpoints do not trigger recovery flow
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  recoverSessionAfterAuthFailure: vi.fn(),
  invalidateSessionAndRedirect: vi.fn(),
}));

vi.mock("@/lib/auth-session", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-session")>("@/lib/auth-session");
  return {
    ...actual,
    recoverSessionAfterAuthFailure: mocks.recoverSessionAfterAuthFailure,
    invalidateSessionAndRedirect: mocks.invalidateSessionAndRedirect,
  };
});

import { apiFetch } from "@/lib/api-config";

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

describe("apiFetch auth recovery", () => {
  beforeEach(() => {
    installStorageMock();
    mocks.recoverSessionAfterAuthFailure.mockReset();
    mocks.invalidateSessionAndRedirect.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("retries once after successful centralized recovery on 401", async () => {
    localStorage.setItem("access_token", "token-1");
    mocks.recoverSessionAfterAuthFailure.mockResolvedValue(true);

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("unauthorized", { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await apiFetch("/api/tasks/");
    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(mocks.recoverSessionAfterAuthFailure).toHaveBeenCalledTimes(1);
    expect(mocks.recoverSessionAfterAuthFailure).toHaveBeenCalledWith(
      expect.objectContaining({
        source: "http_401",
        endpoint: "/api/tasks/",
        method: "GET",
      }),
    );
  });

  it("does not trigger centralized recovery for login endpoint failures", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response("invalid creds", { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await apiFetch("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: "x", password: "y" }),
    });

    expect(response.status).toBe(401);
    expect(mocks.recoverSessionAfterAuthFailure).not.toHaveBeenCalled();
  });

  it("returns original 401 when recovery fails", async () => {
    localStorage.setItem("access_token", "token-1");
    mocks.recoverSessionAfterAuthFailure.mockResolvedValue(false);

    const fetchMock = vi.fn().mockResolvedValueOnce(new Response("unauthorized", { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await apiFetch("/api/tasks/");
    expect(response.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(mocks.recoverSessionAfterAuthFailure).toHaveBeenCalledTimes(1);
  });
});
