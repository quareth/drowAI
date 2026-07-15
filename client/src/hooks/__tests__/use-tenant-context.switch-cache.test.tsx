// @vitest-environment jsdom
/**
 * Verifies tenant switching drops tenant-scoped query caches before UI refresh.
 */

import React from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  TenantContextProvider,
  useTenantContext,
  type TenantMembershipSummary,
} from "@/hooks/use-tenant-context";
import { queryClient } from "@/lib/queryClient";
import { resetStoredActiveTenantContext } from "@/lib/tenant-context";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  authState: {
    isLoading: false,
    user: null as Record<string, unknown> | null,
  },
}));

const MEMBERSHIPS: TenantMembershipSummary[] = [
  {
    membership_id: 101,
    tenant_id: 11,
    tenant_slug: "tenant-a",
    tenant_name: "Tenant A",
    role: "owner",
    membership_status: "active",
    tenant_status: "active",
    is_default_tenant: false,
  },
  {
    membership_id: 102,
    tenant_id: 22,
    tenant_slug: "tenant-b",
    tenant_name: "Tenant B",
    role: "owner",
    membership_status: "active",
    tenant_status: "active",
    is_default_tenant: false,
  },
];

vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => mocked.authState,
}));

function authenticatedUser(): Record<string, unknown> {
  return {
      active_tenant: {
        tenant_id: 11,
        membership_id: 101,
        role: "owner",
        is_default_tenant: false,
        source: "user",
      },
      membership_summaries: MEMBERSHIPS,
      effective_permissions: {
        actions: ["tasks.read", "tasks.write"],
        role: "owner",
        tenant_id: 11,
        policy_version: "v1",
      },
  };
}

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

describe("TenantContextProvider cache invalidation", () => {
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

  beforeEach(() => {
    queryClient.clear();
    installStorageMock();
    localStorage.removeItem("active_tenant_id");
    localStorage.setItem("active_tenant_id", "11");
    mocked.authState = {
      isLoading: false,
      user: authenticatedUser(),
    };
    mocked.apiFetch.mockReset();
    mocked.apiFetch.mockResolvedValue(
      new Response(
        JSON.stringify({
          active_tenant: {
            tenant_id: 22,
            membership_id: 102,
            role: "owner",
            is_default_tenant: false,
            source: "user",
          },
          membership_summaries: MEMBERSHIPS,
          effective_permissions: {
            actions: ["tasks.read", "tasks.write"],
            role: "owner",
            tenant_id: 22,
            policy_version: "v1",
          },
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("preserves the stored tenant while authentication is still loading", () => {
    mocked.authState = { isLoading: true, user: null };
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <TenantContextProvider>{children}</TenantContextProvider>
      </QueryClientProvider>
    );

    renderHook(() => useTenantContext(), { wrapper });

    expect(localStorage.getItem("active_tenant_id")).toBe("11");
  });

  it("removes stale tenant-scoped caches when the active tenant changes", async () => {
    queryClient.setQueryData(["/api/auth/me"], {
      active_tenant: { tenant_id: 11 },
      membership_summaries: MEMBERSHIPS,
      effective_permissions: { tenant_id: 11, actions: [] },
    });
    queryClient.setQueryData(["/api/tasks/"], [{ id: 1 }]);
    queryClient.setQueryData(["knowledge", "summary"], { findings: 1 });
    queryClient.setQueryData(["engagements"], { items: [{ id: "eng-1" }] });
    queryClient.setQueryData(["/api/reports"], [{ id: 91 }]);
    queryClient.setQueryData(["usage-insights", "overview", 1, {}], { totals: {} });
    queryClient.setQueryData(["tasks", "selector"], [{ id: 11 }]);
    queryClient.setQueryData(["files", 11, "tree"], { path: ".", children: [] });
    queryClient.setQueryData(["files", 11, "search", "secret"], { results: [] });
    queryClient.setQueryData(["files", 11, "content", "notes.txt"], { content: "tenant-a" });

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <TenantContextProvider>{children}</TenantContextProvider>
      </QueryClientProvider>
    );

    const { result } = renderHook(() => useTenantContext(), { wrapper });

    await act(async () => {
      await result.current.switchTenant(22);
    });

    await waitFor(() => {
      expect(queryClient.getQueryData(["/api/tasks/"])).toBeUndefined();
      expect(queryClient.getQueryData(["knowledge", "summary"])).toBeUndefined();
      expect(queryClient.getQueryData(["engagements"])).toBeUndefined();
      expect(queryClient.getQueryData(["/api/reports"])).toBeUndefined();
      expect(
        queryClient.getQueryData(["usage-insights", "overview", 1, {}]),
      ).toBeUndefined();
      expect(queryClient.getQueryData(["tasks", "selector"])).toBeUndefined();
      expect(queryClient.getQueryData(["files", 11, "tree"])).toBeUndefined();
      expect(queryClient.getQueryData(["files", 11, "search", "secret"])).toBeUndefined();
      expect(queryClient.getQueryData(["files", 11, "content", "notes.txt"])).toBeUndefined();
    });

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/tenants/context/switch",
      expect.objectContaining({
        method: "POST",
      }),
    );
    expect((queryClient.getQueryData(["/api/auth/me"]) as Record<string, any>)?.active_tenant?.tenant_id).toBe(22);
  });

  it("removes stale tenant caches and cached permissions after tenant-context recovery", async () => {
    queryClient.setQueryData(["/api/auth/me"], {
      active_tenant: { tenant_id: 11 },
      membership_summaries: MEMBERSHIPS,
      effective_permissions: { tenant_id: 11, actions: ["tasks.read"] },
    });
    queryClient.setQueryData(["/api/tasks/"], [{ id: 1 }]);
    queryClient.setQueryData(["knowledge", "summary"], { findings: 1 });
    queryClient.setQueryData(["tasks", "selector"], [{ id: 11 }]);
    queryClient.setQueryData(["files", 11, "tree"], { path: ".", children: [] });
    queryClient.setQueryData(["files", 11, "content", "notes.txt"], { content: "tenant-a" });

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <TenantContextProvider>{children}</TenantContextProvider>
      </QueryClientProvider>
    );

    renderHook(() => useTenantContext(), { wrapper });

    await act(async () => {
      resetStoredActiveTenantContext();
    });

    await waitFor(() => {
      expect(queryClient.getQueryData(["/api/tasks/"])).toBeUndefined();
      expect(queryClient.getQueryData(["knowledge", "summary"])).toBeUndefined();
      expect(queryClient.getQueryData(["tasks", "selector"])).toBeUndefined();
      expect(queryClient.getQueryData(["files", 11, "tree"])).toBeUndefined();
      expect(queryClient.getQueryData(["files", 11, "content", "notes.txt"])).toBeUndefined();
      const authMe = queryClient.getQueryData(["/api/auth/me"]) as Record<string, any>;
      expect(authMe.active_tenant).toBeNull();
      expect(authMe.effective_permissions).toBeNull();
    });
  });
});
