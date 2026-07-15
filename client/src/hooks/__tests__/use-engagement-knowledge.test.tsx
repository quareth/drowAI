// @vitest-environment jsdom
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  engagementKnowledgeKeys,
  invalidateEngagementKnowledgeQueries,
  useCreateEngagement,
  useEngagement,
  useEngagementFinding,
  useEngagementFindings,
  useEngagementKnowledgeRefresh,
  useEngagementWebSurfaceOrigins,
  useEngagementWebSurfacePathPage,
  useEngagements,
  useEngagementSummary,
} from "@/hooks/use-engagement-knowledge";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  apiRequest: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: mocked.apiRequest,
}));

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  mocked.apiFetch.mockReset();
  mocked.apiRequest.mockReset();
});

describe("use-engagement-knowledge", () => {
  it("uses stable query key builders for runner_control routes", () => {
    expect(engagementKnowledgeKeys.engagements()).toEqual(["engagements"]);
    expect(engagementKnowledgeKeys.engagements({ offset: 0, query: "alpha" })).toEqual([
      "engagements",
      { offset: 0, query: "alpha" },
    ]);
    expect(engagementKnowledgeKeys.engagement("42")).toEqual(["engagement", "42"]);
    expect(engagementKnowledgeKeys.summary("42")).toEqual(["engagement", "42", "summary"]);
    expect(
      engagementKnowledgeKeys.findings("42", { severity: "high", limit: 20 }),
    ).toEqual(["engagement", "42", "findings", { limit: 20, severity: "high" }]);
    expect(engagementKnowledgeKeys.assets("42", { exploited: true })).toEqual([
      "engagement",
      "42",
      "assets",
      { exploited: true },
    ]);
    expect(engagementKnowledgeKeys.evidence("42", { source_tool: "nmap" })).toEqual([
      "engagement",
      "42",
      "evidence",
      { source_tool: "nmap" },
    ]);
    expect(engagementKnowledgeKeys.graph("42")).toEqual(["engagement", "42", "graph"]);
    expect(
      engagementKnowledgeKeys.webSurfaceOrigins(
        "42",
        "service.socket:10.0.0.10/tcp/443",
        true,
      ),
    ).toEqual([
      "engagement",
      "42",
      "web-surface",
      "origins",
      "service.socket:10.0.0.10/tcp/443",
      { include_noisy: true },
    ]);
    expect(
      engagementKnowledgeKeys.webSurfacePaths("42", "service.socket:10.0.0.10/tcp/443", {
        include_noisy: true,
        limit: 50,
        offset: 10,
        origin_key: "https://example.com",
      }),
    ).toEqual([
      "engagement",
      "42",
      "web-surface",
      "paths",
      "service.socket:10.0.0.10/tcp/443",
      {
        include_noisy: true,
        limit: 50,
        offset: 10,
        origin_key: "https://example.com",
      },
    ]);
  });

  it("issues authenticated summary request through apiFetch", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        engagement_id: 7,
        open_findings_total: 1,
        open_findings_by_severity: { critical: 1 },
        asset_counts: { total: 1, vulnerable: 1, exploited: 0 },
        service_count: 1,
        evidence_count: 1,
        relationship_count: 1,
        last_observed_at: null,
        open_statuses: ["open"],
      }),
    } as Response);

    const { result } = renderHook(() => useEngagementSummary("7"), { wrapper });

    await waitFor(() => {
      expect(result.current.data?.engagement_id).toBe(7);
    });

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/engagements/7/summary",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("enables engagement detail query only when route param is present", async () => {
    const { rerender } = renderHook(
      ({ id }: { id: string | undefined }) => useEngagement(id),
      {
        initialProps: { id: undefined },
        wrapper,
      },
    );

    await waitFor(() => {
      expect(mocked.apiFetch).not.toHaveBeenCalled();
    });

    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: 12, user_id: 1, name: "E-12" }),
    } as Response);

    rerender({ id: "12" });
    await waitFor(() => {
      expect(mocked.apiFetch).toHaveBeenCalledWith(
        "/api/engagements/12",
        expect.objectContaining({ method: "GET" }),
      );
    });
  });

  it("normalizes findings filter params into deterministic request query", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [], total: 0, limit: 20, offset: 0 }),
    } as Response);

    renderHook(
      () =>
        useEngagementFindings("5", {
          limit: 20,
          severity: "critical",
          offset: 0,
        }),
      { wrapper },
    );

    await waitFor(() => {
      expect(mocked.apiFetch).toHaveBeenCalledTimes(1);
    });

    const calledUrl = String(mocked.apiFetch.mock.calls[0]?.[0] ?? "");
    expect(calledUrl).toContain("/api/engagements/5/findings?");
    expect(calledUrl).toContain("limit=20");
    expect(calledUrl).toContain("offset=0");
    expect(calledUrl).toContain("severity=critical");
  });

  it("encodes engagement finding ids before using them as route segments", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "finding/with/slash", engagement_id: 5 }),
    } as Response);

    const { result } = renderHook(
      () => useEngagementFinding("5", "finding/with/slash"),
      { wrapper },
    );

    await waitFor(() => {
      expect(result.current.data?.id).toBe("finding/with/slash");
    });

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/engagements/5/findings/finding%2Fwith%2Fslash",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("passes engagement status filter to list endpoint", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [], total: 0, limit: 20, offset: 0 }),
    } as Response);

    renderHook(() => useEngagements({ status: "all", limit: 20, offset: 0 }), { wrapper });

    await waitFor(() => {
      expect(mocked.apiFetch).toHaveBeenCalledTimes(1);
    });

    const calledUrl = String(mocked.apiFetch.mock.calls[0]?.[0] ?? "");
    expect(calledUrl).toContain("/api/engagements?");
    expect(calledUrl).toContain("status=all");
  });

  it("uses service-key web-surface hooks and tolerates empty payloads", async () => {
    mocked.apiFetch
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({}),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({}),
      } as Response);

    const { result: originsResult } = renderHook(
      () =>
        useEngagementWebSurfaceOrigins("42", "service.socket:10.0.0.10/tcp/443", {
          include_noisy: true,
        }),
      { wrapper },
    );
    const { result: pathPageResult } = renderHook(
      () =>
        useEngagementWebSurfacePathPage("42", "service.socket:10.0.0.10/tcp/443", {
          include_noisy: true,
          origin_key: "https://example.com",
          limit: 50,
          offset: 0,
        }),
      { wrapper },
    );

    await waitFor(() => {
      expect(originsResult.current.data).toEqual({
        service_key: "service.socket:10.0.0.10/tcp/443",
        items: [],
      });
      expect(pathPageResult.current.data).toEqual({
        service_key: "service.socket:10.0.0.10/tcp/443",
        origin_key: null,
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
        hidden_noisy: 0,
      });
    });

    const firstUrl = String(mocked.apiFetch.mock.calls[0]?.[0] ?? "");
    const secondUrl = String(mocked.apiFetch.mock.calls[1]?.[0] ?? "");
    expect(firstUrl).toContain("/api/engagements/42/web-surface?");
    expect(firstUrl).toContain("service_key=service.socket%3A10.0.0.10%2Ftcp%2F443");
    expect(firstUrl).toContain("include_noisy=true");

    expect(secondUrl).toContain("/api/engagements/42/web-surface/paths?");
    expect(secondUrl).toContain("service_key=service.socket%3A10.0.0.10%2Ftcp%2F443");
    expect(secondUrl).toContain("origin_key=https%3A%2F%2Fexample.com");
    expect(secondUrl).toContain("include_noisy=true");
    expect(secondUrl).toContain("limit=50");
    expect(secondUrl).toContain("offset=0");
  });

  it("invalidates only the targeted engagement cache keys", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    client.setQueryData(engagementKnowledgeKeys.engagements(), { items: [] });
    client.setQueryData(engagementKnowledgeKeys.engagements({ query: "alpha" }), { items: [] });
    client.setQueryData(engagementKnowledgeKeys.engagement("9"), { id: 9 });
    client.setQueryData(engagementKnowledgeKeys.summary("9"), { engagement_id: 9 });
    client.setQueryData(engagementKnowledgeKeys.findings("9", { severity: "high" }), { items: [] });
    client.setQueryData(engagementKnowledgeKeys.assets("9", { exploited: true }), { items: [] });
    client.setQueryData(engagementKnowledgeKeys.evidence("9", { source_tool: "nmap" }), { items: [] });
    client.setQueryData(engagementKnowledgeKeys.graph("9"), { nodes: [], edges: [] });
    client.setQueryData(engagementKnowledgeKeys.webSurfacePrefix("9"), { items: [] });
    client.setQueryData(["/api/tasks/"], [{ id: 1 }]);
    client.setQueryData(engagementKnowledgeKeys.engagement("8"), { id: 8 });

    await invalidateEngagementKnowledgeQueries(client, "9");

    expect(client.getQueryState(engagementKnowledgeKeys.engagements())?.isInvalidated).toBe(true);
    expect(client.getQueryState(engagementKnowledgeKeys.engagements({ query: "alpha" }))?.isInvalidated).toBe(true);
    expect(client.getQueryState(engagementKnowledgeKeys.engagement("9"))?.isInvalidated).toBe(true);
    expect(client.getQueryState(engagementKnowledgeKeys.summary("9"))?.isInvalidated).toBe(true);
    expect(
      client.getQueryState(engagementKnowledgeKeys.findings("9", { severity: "high" }))?.isInvalidated,
    ).toBe(true);
    expect(
      client.getQueryState(engagementKnowledgeKeys.assets("9", { exploited: true }))?.isInvalidated,
    ).toBe(true);
    expect(
      client.getQueryState(engagementKnowledgeKeys.evidence("9", { source_tool: "nmap" }))?.isInvalidated,
    ).toBe(true);
    expect(client.getQueryState(engagementKnowledgeKeys.graph("9"))?.isInvalidated).toBe(true);
    expect(client.getQueryState(engagementKnowledgeKeys.webSurfacePrefix("9"))?.isInvalidated).toBe(true);

    expect(client.getQueryState(["/api/tasks/"])?.isInvalidated).toBe(false);
    expect(client.getQueryState(engagementKnowledgeKeys.engagement("8"))?.isInvalidated).toBe(false);
  });

  it("refresh hook triggers targeted engagement invalidation", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    client.setQueryData(engagementKnowledgeKeys.summary("12"), { engagement_id: 12 });
    client.setQueryData(["/api/tasks/"], [{ id: 2 }]);

    const clientWrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useEngagementKnowledgeRefresh("12"), {
      wrapper: clientWrapper,
    });

    await result.current.refresh();

    expect(client.getQueryState(engagementKnowledgeKeys.summary("12"))?.isInvalidated).toBe(true);
    expect(client.getQueryState(["/api/tasks/"])?.isInvalidated).toBe(false);
  });

  it("rejects create mutation when backend responds with a non-ok status", async () => {
    mocked.apiRequest.mockResolvedValueOnce({
      ok: false,
      status: 400,
      statusText: "Bad Request",
      text: async () => "name is required",
    } as Response);

    const { result } = renderHook(() => useCreateEngagement(), { wrapper });

    await expect(
      result.current.mutateAsync({ name: "", description: "desc" }),
    ).rejects.toThrow("400: name is required");
  });
});
