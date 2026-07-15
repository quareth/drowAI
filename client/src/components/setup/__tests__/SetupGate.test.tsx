// @vitest-environment jsdom
/** Regression coverage for setup-route gating before and after provisioning. */
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SetupGate } from "@/components/setup/SetupGate";

const setLocation = vi.fn();
const routeState = vi.hoisted(() => ({ location: "/" }));

vi.mock("wouter", () => ({
  useLocation: () => [routeState.location, setLocation],
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: vi.fn(),
}));

import { apiRequest } from "@/lib/queryClient";

function renderGate(initialPath = "/") {
  routeState.location = initialPath;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SetupGate>
        <div data-testid="child">app</div>
      </SetupGate>
    </QueryClientProvider>,
  );
}

describe("SetupGate", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.mocked(apiRequest).mockReset();
    setLocation.mockReset();
  });

  it("redirects to setup when installation is required", async () => {
    vi.mocked(apiRequest).mockResolvedValue({
      setup_required: true,
      wizard_enabled: true,
      installation_complete: false,
      installation_status: "pending",
      setup_error: null,
      deployment_profile: "single_host",
      database_accessible: true,
      runner_connected: false,
    });

    renderGate("/");

    await waitFor(() => {
      expect(setLocation).toHaveBeenCalledWith("/setup");
    });
  });

  it("renders children when setup is not required", async () => {
    vi.mocked(apiRequest).mockResolvedValue({
      setup_required: false,
      wizard_enabled: true,
      installation_complete: true,
      installation_status: "complete",
      setup_error: null,
      deployment_profile: "single_host",
      database_accessible: true,
      runner_connected: true,
    });

    const view = renderGate("/");
    await waitFor(() => {
      expect(view.queryByTestId("child")).not.toBeNull();
    });
    expect(setLocation).not.toHaveBeenCalledWith("/setup");
  });

  it("leaves the completed setup route for SetupPage to show its success state", async () => {
    vi.mocked(apiRequest).mockResolvedValue({
      setup_required: false,
      wizard_enabled: true,
      installation_complete: true,
      installation_status: "complete",
      setup_error: null,
      deployment_profile: "single_host",
      database_accessible: true,
      runner_connected: false,
    });

    const view = renderGate("/setup");

    await waitFor(() => expect(view.queryByTestId("child")).not.toBeNull());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(setLocation).not.toHaveBeenCalledWith("/auth");
  });
});
