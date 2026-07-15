/**
 * Tenant query-cache invalidation helpers.
 *
 * Responsibilities:
 * - classify tenant-owned React Query keys that must not survive tenant switches
 * - remove cached tenant-owned query data after the active tenant changes
 */

import type { QueryClient, QueryKey } from "@tanstack/react-query";

function readFirstSegment(queryKey: QueryKey): string | null {
  if (!Array.isArray(queryKey) || queryKey.length === 0) {
    return null;
  }
  const first = queryKey[0];
  if (typeof first !== "string") {
    return null;
  }
  return first;
}

export function isTenantScopedQueryKey(queryKey: QueryKey): boolean {
  const first = readFirstSegment(queryKey);
  if (!first) {
    return false;
  }

  if (first === "/api/auth/me") {
    return false;
  }

  if (
    first.startsWith("/api/tasks") ||
    first.startsWith("/api/reports") ||
    first.startsWith("/api/usage")
  ) {
    return true;
  }

  return (
    first === "knowledge" ||
    first === "engagements" ||
    first === "engagement" ||
    first === "tasks" ||
    first === "files" ||
    first === "usage-insights" ||
    first === "reporting" ||
    first === "task-run-state-batch" ||
    first === "interrupt-state" ||
    first === "reasoning"
  );
}

export function clearTenantScopedQueryCaches(client: QueryClient): void {
  client.removeQueries({
    predicate: (query) => isTenantScopedQueryKey(query.queryKey),
  });
}
