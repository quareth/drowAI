/* Tests for tenant-owned React Query cache classification. */

import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import {
  clearTenantScopedQueryCaches,
  isTenantScopedQueryKey,
} from "@/lib/tenant-query-cache";

describe("tenant query cache classification", () => {
  it("classifies reporting query keys as tenant scoped", () => {
    expect(isTenantScopedQueryKey(["reporting", "inputs", { engagement_id: 7 }])).toBe(
      true,
    );
    expect(isTenantScopedQueryKey(["reporting", "job", { job_id: "job-1" }])).toBe(
      true,
    );
    expect(isTenantScopedQueryKey(["/api/auth/me"])).toBe(false);
  });

  it("clears reporting caches while preserving non-tenant keys", () => {
    const client = new QueryClient();
    client.setQueryData(["reporting", "inputs", { engagement_id: 7 }], {
      tasks: [],
    });
    client.setQueryData(["reporting", "job", { job_id: "job-1" }], {
      status: "queued",
    });
    client.setQueryData(["/api/auth/me"], {
      user: "current",
    });
    client.setQueryData(["theme", "preference"], "dark");

    clearTenantScopedQueryCaches(client);

    expect(
      client.getQueryData(["reporting", "inputs", { engagement_id: 7 }]),
    ).toBeUndefined();
    expect(client.getQueryData(["reporting", "job", { job_id: "job-1" }])).toBeUndefined();
    expect(client.getQueryData(["/api/auth/me"])).toEqual({ user: "current" });
    expect(client.getQueryData(["theme", "preference"])).toBe("dark");
  });
});
