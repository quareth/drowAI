// @vitest-environment jsdom
/**
 * Verifies apiFetch tenant-context behavior:
 * - active tenant hint header is propagated from local storage
 * - stale tenant hints are cleared and request is retried once
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@/lib/api-config";
import { ACTIVE_TENANT_CHANGED_EVENT } from "@/lib/tenant-context";

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

describe("apiFetch tenant context propagation", () => {
  beforeEach(() => {
    installStorageMock();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("adds X-Active-Tenant-Id from active tenant storage", async () => {
    localStorage.setItem("access_token", "token-123");
    localStorage.setItem("active_tenant_id", "23");

    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/tasks/");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, requestOptions] = fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit];
    const headers = new Headers(requestOptions.headers);
    expect(headers.get("X-Active-Tenant-Id")).toBe("23");
  });

  it("clears stale tenant id and retries once on tenant-context policy failure", async () => {
    localStorage.setItem("access_token", "token-abc");
    localStorage.setItem("active_tenant_id", "77");
    const tenantChanged = vi.fn();
    window.addEventListener(ACTIVE_TENANT_CHANGED_EVENT, tenantChanged);

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Requested tenant membership is inactive." }), { status: 403 }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await apiFetch("/api/tasks/");

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem("active_tenant_id")).toBeNull();
    expect(tenantChanged).toHaveBeenCalledTimes(1);
    expect((tenantChanged.mock.calls[0][0] as CustomEvent).detail).toEqual({
      previousTenantId: 77,
      nextTenantId: null,
    });
    window.removeEventListener(ACTIVE_TENANT_CHANGED_EVENT, tenantChanged);

    const [, secondRequestOptions] = fetchMock.mock.calls[1] as [RequestInfo | URL, RequestInit];
    const secondHeaders = new Headers(secondRequestOptions.headers);
    expect(secondHeaders.get("X-Active-Tenant-Id")).toBeNull();
  });
});
