/* Shared react-query hook family for the task-scoped Usage Insights API.
 *
 * Responsibility: provide ONE set of thin, typed GET hooks + shared query-key /
 * query-string helpers so the Usage page's cards, charts, and table can all
 * read from the four insights endpoints without inventing parallel fetch
 * logic. Hooks never derive numeric fields — server payloads are returned
 * verbatim (see ownership checklist: single-hook-family,
 * no-frontend-cost-math, server-side-derived-metrics).
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api-config";
import type {
  GroupByKey,
  UsageInsightsFilters,
  UsageInsightsGroupsResponse,
  UsageInsightsOverviewResponse,
  UsageInsightsRecordsResponse,
  UsageInsightsTimelineResponse,
} from "@/types/usage";

/** Endpoint tag baked into every query key. Keep this narrow so Phase 4's
 *  cleanup greps can audit the hook family cheaply. */
type InsightsEndpoint = "overview" | "groups" | "timeline" | "records";

/** Non-filter extras carried in the query key / URL (e.g. groupBy, page). */
type InsightsExtras = Readonly<Record<string, string | number>>;

/**
 * Staleness chosen deliberately for the Usage page: a task's LLM usage
 * trails behind live streaming, but the Usage page is read-only — we want
 * cards/charts/table refetched when the user revisits without re-hitting the
 * network on every interaction. 10 seconds matches typical insights freshness
 * for a read-only insights surface and is looser than the legacy compact
 * summary (`staleTime: Infinity`, invalidated explicitly on stream end) since
 * we have no such event for the Usage page yet.
 */
const INSIGHTS_STALE_TIME_MS = 10_000;

/**
 * Drop undefined / null / empty-string entries, sort remaining keys, and
 * stringify values. This same normalized object is folded into both the query
 * key and the URL query string so identical logical inputs always collapse to
 * the same cache entry (see invariant: stable-naming).
 */
function normalizeFiltersForQuery(
  filters: UsageInsightsFilters | undefined,
  extras: InsightsExtras | undefined,
): Record<string, string> {
  const merged: Record<string, unknown> = {
    ...(filters ?? {}),
    ...(extras ?? {}),
  };
  const normalized: Record<string, string> = {};
  for (const key of Object.keys(merged).sort()) {
    const value = merged[key];
    if (value === undefined || value === null) continue;
    const stringValue = String(value);
    // `filters` are forwarded verbatim — empty strings are dropped so they
    // do not masquerade as an explicit "unknown" bucket filter. Callers who
    // want to filter on the explicit-unknown bucket must pass "unknown".
    if (stringValue === "") continue;
    normalized[key] = stringValue;
  }
  return normalized;
}

/**
 * Build a stable, filter-aware react-query key.
 *
 * Shape: `["usage-insights", endpoint, taskId, { ...sortedFilters, ...extras }]`.
 * The trailing object is the deterministic fingerprint produced by
 * {@link normalizeFiltersForQuery} — react-query performs deep-equal on the
 * key, so any two calls with the same logical inputs share a cache entry.
 *
 * Exported so unit tests can verify determinism without mounting hooks.
 */
export function buildInsightsQueryKey(
  endpoint: InsightsEndpoint,
  taskId: number,
  filters?: UsageInsightsFilters,
  extras?: InsightsExtras,
): readonly [string, InsightsEndpoint, number, Record<string, string>] {
  return [
    "usage-insights",
    endpoint,
    taskId,
    normalizeFiltersForQuery(filters, extras),
  ] as const;
}

/**
 * Serialize filters + extras into a URL query-string suffix.
 *
 * - Returns an empty string when there is nothing to encode.
 * - Otherwise returns a leading `?` followed by a `URLSearchParams` encoding
 *   with keys sorted alphabetically. This matches the query-key fingerprint
 *   so server/proxy caches see the same canonical URL for the same inputs.
 *
 * Exported for direct unit testing.
 */
export function buildInsightsQueryString(
  filters?: UsageInsightsFilters,
  extras?: InsightsExtras,
): string {
  const normalized = normalizeFiltersForQuery(filters, extras);
  const keys = Object.keys(normalized);
  if (keys.length === 0) return "";
  const params = new URLSearchParams();
  for (const key of keys) {
    params.set(key, normalized[key]);
  }
  return `?${params.toString()}`;
}

/** Shared GET wrapper — threads the react-query AbortSignal through apiFetch
 *  and raises a text-suffixed Error on non-2xx so consumers see status codes. */
async function fetchInsightsJson<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch(endpoint, { method: "GET", signal });
  if (!response.ok) {
    const details = await response.text().catch(() => "");
    throw new Error(`${response.status}: ${details || response.statusText}`);
  }
  return response.json() as Promise<T>;
}

/** Internal helper: when `taskId` is null/undefined we still need a stable
 *  (but unused) key shape for react-query. The `enabled` flag blocks the
 *  actual request from firing. */
function disabledKey(
  endpoint: InsightsEndpoint,
): readonly [string, InsightsEndpoint, "__disabled__"] {
  return ["usage-insights", endpoint, "__disabled__"] as const;
}

/**
 * GET /api/tasks/{task_id}/usage/insights/overview
 *
 * Returns null/undefined-safe result: request is disabled when `taskId` is
 * missing; react-query leaves `data` as `undefined` in that state.
 */
export function useUsageInsightsOverview(
  taskId: number | null | undefined,
  filters?: UsageInsightsFilters,
): UseQueryResult<UsageInsightsOverviewResponse> {
  const enabled = taskId != null;
  return useQuery<UsageInsightsOverviewResponse>({
    queryKey: enabled
      ? buildInsightsQueryKey("overview", taskId as number, filters)
      : disabledKey("overview"),
    enabled,
    staleTime: INSIGHTS_STALE_TIME_MS,
    queryFn: ({ signal }) =>
      fetchInsightsJson<UsageInsightsOverviewResponse>(
        `/api/tasks/${taskId}/usage/insights/overview${buildInsightsQueryString(filters)}`,
        signal,
      ),
  });
}

/**
 * GET /api/tasks/{task_id}/usage/insights/groups?group_by=<GroupByKey>
 *
 * `groupBy` is required by the backend; it rides in both the query key and
 * the URL string via the shared extras channel.
 */
export function useUsageInsightsGroups(
  taskId: number | null | undefined,
  groupBy: GroupByKey,
  filters?: UsageInsightsFilters,
): UseQueryResult<UsageInsightsGroupsResponse> {
  const enabled = taskId != null;
  const extras: InsightsExtras = { group_by: groupBy };
  return useQuery<UsageInsightsGroupsResponse>({
    queryKey: enabled
      ? buildInsightsQueryKey("groups", taskId as number, filters, extras)
      : [...disabledKey("groups"), groupBy],
    enabled,
    staleTime: INSIGHTS_STALE_TIME_MS,
    queryFn: ({ signal }) =>
      fetchInsightsJson<UsageInsightsGroupsResponse>(
        `/api/tasks/${taskId}/usage/insights/groups${buildInsightsQueryString(filters, extras)}`,
        signal,
      ),
  });
}

/**
 * GET /api/tasks/{task_id}/usage/insights/timeline
 *
 * Backend ships chronological per-call points already; no client-side
 * aggregation is performed (see invariant: simple-timeline-shape).
 */
export function useUsageInsightsTimeline(
  taskId: number | null | undefined,
  filters?: UsageInsightsFilters,
): UseQueryResult<UsageInsightsTimelineResponse> {
  const enabled = taskId != null;
  return useQuery<UsageInsightsTimelineResponse>({
    queryKey: enabled
      ? buildInsightsQueryKey("timeline", taskId as number, filters)
      : disabledKey("timeline"),
    enabled,
    staleTime: INSIGHTS_STALE_TIME_MS,
    queryFn: ({ signal }) =>
      fetchInsightsJson<UsageInsightsTimelineResponse>(
        `/api/tasks/${taskId}/usage/insights/timeline${buildInsightsQueryString(filters)}`,
        signal,
      ),
  });
}

/**
 * GET /api/tasks/{task_id}/usage/insights/records?page=&page_size=
 *
 * Pagination is server-owned; `page` and `pageSize` are folded into both the
 * query key and the URL query string so page flips produce distinct cache
 * entries (and the previous page stays cached on back-navigation).
 */
export function useUsageInsightsRecords(
  taskId: number | null | undefined,
  page: number,
  pageSize: number,
  filters?: UsageInsightsFilters,
): UseQueryResult<UsageInsightsRecordsResponse> {
  const enabled = taskId != null;
  const extras: InsightsExtras = { page, page_size: pageSize };
  return useQuery<UsageInsightsRecordsResponse>({
    queryKey: enabled
      ? buildInsightsQueryKey("records", taskId as number, filters, extras)
      : [...disabledKey("records"), page, pageSize],
    enabled,
    staleTime: INSIGHTS_STALE_TIME_MS,
    queryFn: ({ signal }) =>
      fetchInsightsJson<UsageInsightsRecordsResponse>(
        `/api/tasks/${taskId}/usage/insights/records${buildInsightsQueryString(filters, extras)}`,
        signal,
      ),
  });
}
