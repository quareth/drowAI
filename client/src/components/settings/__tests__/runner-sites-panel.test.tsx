/**
 * Regression coverage for Runner Site readiness status presentation.
 */
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunnerSitesPanel } from "@/components/settings/runner-sites-panel";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

type RunnerSiteFixture = {
  id: string;
  name: string;
  slug: string;
  status: string;
  connectivity_status: string;
  runner_count: number;
  connected_runner_count: number;
  last_seen_at: string | null;
  network_label: string | null;
  labels: Record<string, string>;
  created_at: string;
  updated_at: string;
};

type RunnerReadinessFixture = {
  status: string;
  ready: boolean;
  reason_codes: string[];
  runner_site_count: number;
  connected_runner_count: number;
  evaluated_runner_count: number;
  selected_runner_id: string | null;
  execution_site_id: string | null;
};

const SITE_ID = "0d9bb1a3-78e4-4c45-a21e-2e742808a4dc";
const SECONDARY_SITE_ID = "83c61cd5-c8ac-4b29-973f-e46e8aa1f040";

function makeSite(overrides: Partial<RunnerSiteFixture> = {}): RunnerSiteFixture {
  return {
    id: SITE_ID,
    name: "Primary Runner Site",
    slug: "primary-runner-site",
    status: "active",
    connectivity_status: "waiting",
    runner_count: 0,
    connected_runner_count: 0,
    last_seen_at: null,
    network_label: null,
    labels: {},
    created_at: "2026-07-09T12:00:00Z",
    updated_at: "2026-07-09T12:00:00Z",
    ...overrides,
  };
}

function makeReadiness(overrides: Partial<RunnerReadinessFixture> = {}): RunnerReadinessFixture {
  return {
    status: "waiting_for_runner",
    ready: false,
    reason_codes: ["NO_RUNNERS_REGISTERED"],
    runner_site_count: 1,
    connected_runner_count: 0,
    evaluated_runner_count: 0,
    selected_runner_id: null,
    execution_site_id: SITE_ID,
    ...overrides,
  };
}

function renderPanel(
  siteOrSites: RunnerSiteFixture | RunnerSiteFixture[],
  readinessOrReadinessBySite: RunnerReadinessFixture | Record<string, RunnerReadinessFixture>,
) {
  const sites = Array.isArray(siteOrSites) ? siteOrSites : [siteOrSites];
  const readinessBySite =
    "status" in readinessOrReadinessBySite
      ? { [readinessOrReadinessBySite.execution_site_id ?? SITE_ID]: readinessOrReadinessBySite }
      : readinessOrReadinessBySite;
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        queryFn: ({ queryKey }) => {
          const endpoint = String(queryKey[0]);
          if (endpoint === "/api/runner-control/runner-sites") {
            return Promise.resolve(sites);
          }
          if (endpoint === "/api/runner-control/management-url") {
            return Promise.resolve({
              management_url: "http://management.example.test",
              source: "request_origin",
            });
          }
          if (endpoint.startsWith("/api/runner-control/readiness?")) {
            const siteId = new URL(endpoint, window.location.origin).searchParams.get("execution_site_id");
            const readiness = siteId ? readinessBySite[siteId] : undefined;
            if (readiness) {
              return Promise.resolve(readiness);
            }
          }
          throw new Error(`Unexpected query endpoint: ${endpoint}`);
        },
      },
      mutations: { retry: false },
    },
  });

  const rendered = render(
    <QueryClientProvider client={client}>
      <RunnerSitesPanel />
    </QueryClientProvider>,
  );
  return { ...rendered, client };
}

function mockResponse(status: number, body?: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
    text: vi.fn().mockResolvedValue(body == null ? "" : JSON.stringify(body)),
  } as unknown as Response;
}

afterEach(() => {
  cleanup();
  mocked.apiFetch.mockReset();
  vi.restoreAllMocks();
});

describe("<RunnerSitesPanel />", () => {
  it("does not show a site without connected runners as ready", async () => {
    renderPanel(makeSite(), makeReadiness());

    expect(await screen.findByDisplayValue("http://management.example.test")).toBeTruthy();
    expect(await screen.findByText("Waiting for Runner")).toBeTruthy();
    expect(screen.queryByText("Site: Active")).toBeNull();
    expect(screen.getByText("Registered: 0")).toBeTruthy();
    expect(screen.getByText("Connected: 0")).toBeTruthy();
    expect(screen.getByText("No Runners are registered for this Runner Site.")).toBeTruthy();
    expect(screen.queryByText(/Ready /)).toBeNull();
  });

  it("shows an empty Runner Site as waiting even when another site has a Runner", async () => {
    const emptySite = makeSite();
    const connectedSite = makeSite({
      id: SECONDARY_SITE_ID,
      name: "Secondary Runner Site",
      slug: "secondary-runner-site",
      connectivity_status: "connected",
      runner_count: 1,
      connected_runner_count: 1,
    });

    renderPanel([emptySite, connectedSite], {
      [SITE_ID]: makeReadiness({
        status: "runner_incompatible",
        reason_codes: ["RUNNER_EXECUTION_SITE_MISMATCH"],
        runner_site_count: 2,
        connected_runner_count: 1,
        evaluated_runner_count: 1,
      }),
      [SECONDARY_SITE_ID]: makeReadiness({
        status: "ready",
        ready: true,
        reason_codes: [],
        runner_site_count: 2,
        connected_runner_count: 1,
        evaluated_runner_count: 1,
        selected_runner_id: "fc855124-8ef6-4f64-9105-b9cd36e6e246",
        execution_site_id: SECONDARY_SITE_ID,
      }),
    });

    expect(await screen.findByText("Waiting for Runner")).toBeTruthy();
    expect(screen.getByText("Ready 1/1")).toBeTruthy();
    expect(screen.getByText("No Runners are registered for this Runner Site.")).toBeTruthy();
    expect(screen.queryByText("Not compatible")).toBeNull();
  });

  it("shows a registered offline runner as offline, not connected", async () => {
    renderPanel(
      makeSite({
        connectivity_status: "offline",
        runner_count: 1,
        connected_runner_count: 0,
      }),
      makeReadiness({
        status: "runner_registered_offline",
        reason_codes: ["RUNNER_STALE_OR_OFFLINE"],
        evaluated_runner_count: 1,
      }),
    );

    expect(await screen.findByText("Offline 0/1")).toBeTruthy();
    expect(screen.getByText("Registered: 1")).toBeTruthy();
    expect(screen.getByText("Connected: 0")).toBeTruthy();
    expect(screen.getByText("Runners are registered, but no live connection is available for task runtime work.")).toBeTruthy();
    expect(screen.queryByText("Connected 0/1")).toBeNull();
  });

  it("shows capacity exhaustion distinctly from offline or missing runners", async () => {
    renderPanel(
      makeSite({
        connectivity_status: "connected",
        runner_count: 1,
        connected_runner_count: 1,
      }),
      makeReadiness({
        status: "waiting_for_runner",
        reason_codes: ["RUNNER_CAPACITY_EXHAUSTED"],
        connected_runner_count: 1,
        evaluated_runner_count: 1,
      }),
    );

    expect(await screen.findByText("Capacity full")).toBeTruthy();
    expect(screen.getByText("Runner capacity is exhausted. Existing Runners are connected but have no task slots available.")).toBeTruthy();
    expect(screen.queryByText("Waiting for Runner")).toBeNull();
    expect(screen.queryByText(/Offline /)).toBeNull();
  });

  it("hard-removes a site after warning about unreachable Runner hosts", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const response = mockResponse(204);
    mocked.apiFetch.mockResolvedValue(response);
    const { client } = renderPanel(
      makeSite({
        connectivity_status: "offline",
        runner_count: 2,
        connected_runner_count: 0,
      }),
      makeReadiness({
        status: "runner_registered_offline",
        reason_codes: ["RUNNER_STALE_OR_OFFLINE"],
        evaluated_runner_count: 2,
      }),
    );
    const invalidateQueries = vi.spyOn(client, "invalidateQueries").mockResolvedValue();

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => {
      expect(mocked.apiFetch).toHaveBeenCalledWith(
        `/api/runner-control/runner-sites/${SITE_ID}`,
        { method: "DELETE" },
      );
    });
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("Permanently remove Runner Site"));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("cannot be stopped by Management"));
    expect(response.json).not.toHaveBeenCalled();
    expect(client.getQueryData<RunnerSiteFixture[]>(["/api/runner-control/runner-sites"])).toEqual([]);
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["/api/runner-control/runner-sites"] });
  });

  it("shows an actionable active-execution conflict without removing the site", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mocked.apiFetch.mockResolvedValue(mockResponse(409, {
      detail: {
        error_code: "RUNNER_SITE_ACTIVE_EXECUTIONS",
        message: "Runner Site has active executions.",
        execution_count: 3,
      },
    }));
    const { client } = renderPanel(
      makeSite({
        connectivity_status: "connected",
        runner_count: 1,
        connected_runner_count: 1,
      }),
      makeReadiness({
        status: "ready",
        ready: true,
        reason_codes: [],
        connected_runner_count: 1,
        evaluated_runner_count: 1,
      }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    expect(await screen.findByText(/RUNNER_SITE_ACTIVE_EXECUTIONS:.*3 active executions.*Stop them/)).toBeTruthy();
    expect(client.getQueryData<RunnerSiteFixture[]>(["/api/runner-control/runner-sites"])).toHaveLength(1);
  });

  it("requires replacement capacity when removing the last connected Runner Site", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mocked.apiFetch.mockResolvedValue(mockResponse(409, {
      detail: {
        error_code: "RUNNER_SITE_LAST_CONNECTED",
        message: "Another connected Runner must remain.",
      },
    }));
    renderPanel(
      makeSite({
        connectivity_status: "connected",
        runner_count: 1,
        connected_runner_count: 1,
      }),
      makeReadiness({
        status: "ready",
        ready: true,
        reason_codes: [],
        connected_runner_count: 1,
        evaluated_runner_count: 1,
      }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    expect(await screen.findByText(/RUNNER_SITE_LAST_CONNECTED:.*Connect another Runner Site/)).toBeTruthy();
  });
});
