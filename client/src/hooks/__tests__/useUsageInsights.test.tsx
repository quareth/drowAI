// @vitest-environment jsdom
/* Unit tests for the shared Usage Insights hook family.
 *
 * Focus:
 *  - buildInsightsQueryKey: determinism across property order, divergence on
 *    different inputs.
 *  - buildInsightsQueryString: encoding, omission of undefined/null/empty,
 *    URL-safety of special characters.
 *  - Each hook is disabled (no request fires) when taskId is null/undefined.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildInsightsQueryKey,
  buildInsightsQueryString,
  useUsageInsightsGroups,
  useUsageInsightsOverview,
  useUsageInsightsRecords,
  useUsageInsightsTimeline,
} from "@/hooks/useUsageInsights";
import type { UsageInsightsFilters } from "@/types/usage";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

afterEach(() => {
  mocked.apiFetch.mockReset();
});

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("buildInsightsQueryKey", () => {
  it("always starts with the ['usage-insights', endpoint, taskId] prefix", () => {
    const key = buildInsightsQueryKey("overview", 42);
    expect(key[0]).toBe("usage-insights");
    expect(key[1]).toBe("overview");
    expect(key[2]).toBe(42);
    expect(key[3]).toEqual({});
  });

  it("produces identical keys regardless of filter property order", () => {
    const a: UsageInsightsFilters = { provider: "openai", model: "gpt-5" };
    const b: UsageInsightsFilters = { model: "gpt-5", provider: "openai" };
    expect(buildInsightsQueryKey("groups", 7, a, { group_by: "role" })).toEqual(
      buildInsightsQueryKey("groups", 7, b, { group_by: "role" }),
    );
  });

  it("produces different keys when any filter value differs", () => {
    const keyA = buildInsightsQueryKey("overview", 7, { provider: "openai" });
    const keyB = buildInsightsQueryKey("overview", 7, { provider: "anthropic" });
    expect(keyA).not.toEqual(keyB);
  });

  it("produces different keys for different endpoints with the same filters", () => {
    const filters: UsageInsightsFilters = { role: "planner" };
    expect(buildInsightsQueryKey("overview", 3, filters)).not.toEqual(
      buildInsightsQueryKey("timeline", 3, filters),
    );
  });

  it("produces different keys for different taskIds", () => {
    const filters: UsageInsightsFilters = { role: "planner" };
    expect(buildInsightsQueryKey("overview", 1, filters)).not.toEqual(
      buildInsightsQueryKey("overview", 2, filters),
    );
  });

  it("omits undefined / null / empty-string filter entries from the fingerprint", () => {
    const key = buildInsightsQueryKey("overview", 5, {
      provider: "openai",
      model: undefined,
      role: "",
      // `conversation_id: null` is not expressible in the type, but the
      // normalizer still tolerates it at runtime for defensive parity.
    });
    expect(key[3]).toEqual({ provider: "openai" });
  });

  it("merges extras into the fingerprint deterministically", () => {
    const keyA = buildInsightsQueryKey(
      "records",
      9,
      { provider: "openai" },
      { page: 2, page_size: 50 },
    );
    const keyB = buildInsightsQueryKey(
      "records",
      9,
      { provider: "openai" },
      { page_size: 50, page: 2 },
    );
    expect(keyA).toEqual(keyB);
  });

  it("preserves the literal 'unknown' bucket filter value", () => {
    // explicit-unknown-buckets: "unknown" is a real filter value, not a drop
    // candidate. The hook must forward it verbatim.
    const key = buildInsightsQueryKey("groups", 11, { provider: "unknown" });
    expect(key[3]).toEqual({ provider: "unknown" });
  });
});

describe("buildInsightsQueryString", () => {
  it("returns an empty string when there is nothing to encode", () => {
    expect(buildInsightsQueryString(undefined, undefined)).toBe("");
    expect(buildInsightsQueryString({}, {})).toBe("");
  });

  it("encodes provided filter values with a leading '?'", () => {
    const qs = buildInsightsQueryString({ provider: "openai", model: "gpt-5" });
    // Keys are sorted alphabetically (model before provider).
    expect(qs).toBe("?model=gpt-5&provider=openai");
  });

  it("omits undefined / empty-string filter entries from the URL", () => {
    const qs = buildInsightsQueryString({
      provider: "openai",
      model: undefined,
      role: "",
    });
    expect(qs).toBe("?provider=openai");
  });

  it("URL-encodes special characters in filter values", () => {
    const qs = buildInsightsQueryString({ conversation_id: "abc def&xyz" });
    expect(qs).toBe("?conversation_id=abc+def%26xyz");
  });

  it("merges extras (e.g. group_by, page) alongside filters in a sorted suffix", () => {
    const qs = buildInsightsQueryString(
      { provider: "openai" },
      { group_by: "role", page: 1, page_size: 25 },
    );
    expect(qs).toBe("?group_by=role&page=1&page_size=25&provider=openai");
  });
});

describe("useUsageInsights hooks with taskId null/undefined", () => {
  it("useUsageInsightsOverview does not fire a request when taskId is null", () => {
    renderHook(() => useUsageInsightsOverview(null), { wrapper });
    expect(mocked.apiFetch).not.toHaveBeenCalled();
  });

  it("useUsageInsightsOverview does not fire a request when taskId is undefined", () => {
    renderHook(() => useUsageInsightsOverview(undefined), { wrapper });
    expect(mocked.apiFetch).not.toHaveBeenCalled();
  });

  it("useUsageInsightsGroups does not fire a request when taskId is null", () => {
    renderHook(() => useUsageInsightsGroups(null, "role"), { wrapper });
    expect(mocked.apiFetch).not.toHaveBeenCalled();
  });

  it("useUsageInsightsTimeline does not fire a request when taskId is null", () => {
    renderHook(() => useUsageInsightsTimeline(null), { wrapper });
    expect(mocked.apiFetch).not.toHaveBeenCalled();
  });

  it("useUsageInsightsRecords does not fire a request when taskId is null", () => {
    renderHook(() => useUsageInsightsRecords(null, 1, 25), { wrapper });
    expect(mocked.apiFetch).not.toHaveBeenCalled();
  });
});

describe("useUsageInsights hooks with a real taskId", () => {
  it("useUsageInsightsOverview fires a GET at the scoped URL with filters", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task_id: 7,
        provider_coverage: { openai: 3 },
        call_count: 3,
        prompt_tokens: 1000,
        completion_tokens: 500,
        cached_tokens: 200,
        uncached_prompt_tokens: 800,
        cache_hit_calls: 2,
        cache_hit_rate: 0.6667,
        cache_ratio: 0.2,
        cache_reporting_call_count: 3,
        cache_reporting_coverage: 1.0,
        cost_usd: 1.23,
        cached_input_cost_usd: 0.01,
        uncached_input_cost_usd: 0.5,
        output_cost_usd: 0.72,
      }),
    } as Response);

    renderHook(
      () => useUsageInsightsOverview(7, { provider: "openai" }),
      { wrapper },
    );

    // react-query schedules the queryFn microtask; flush it.
    await Promise.resolve();
    await Promise.resolve();

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/tasks/7/usage/insights/overview?provider=openai",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("useUsageInsightsGroups threads group_by into the URL alongside filters", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ task_id: 9, group_by: "role", items: [] }),
    } as Response);

    renderHook(
      () =>
        useUsageInsightsGroups(9, "role", { execution_branch: "main" }),
      { wrapper },
    );

    await Promise.resolve();
    await Promise.resolve();

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/tasks/9/usage/insights/groups?execution_branch=main&group_by=role",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("useUsageInsightsRecords threads page and page_size into the URL", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task_id: 3,
        items: [],
        total_count: 0,
        page: 2,
        page_size: 50,
        has_more: false,
      }),
    } as Response);

    renderHook(() => useUsageInsightsRecords(3, 2, 50), { wrapper });

    await Promise.resolve();
    await Promise.resolve();

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/tasks/3/usage/insights/records?page=2&page_size=50",
      expect.objectContaining({ method: "GET" }),
    );
  });
});
