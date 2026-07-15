/**
 * Regression coverage for setup completion navigation state.
 */
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SetupPage from "@/pages/setup";
import type { SetupCompleteResponse, SetupStatus } from "@/components/setup/setup-types";

const setLocation = vi.fn();
const mocked = vi.hoisted(() => ({
  fetchSetupStatus: vi.fn(),
  completeSetup: vi.fn(),
  skipSetupWizard: vi.fn(),
}));

vi.mock("wouter", () => ({
  useLocation: () => ["/setup", setLocation],
}));

vi.mock("@/components/setup/setup-api", () => mocked);

vi.mock("@/components/setup/welcome-step", () => ({
  WelcomeStep: ({ onNext }: { onNext: () => void }) => (
    <button type="button" onClick={onNext}>
      next-welcome
    </button>
  ),
}));
vi.mock("@/components/setup/database-step", () => ({
  DatabaseStep: ({ onNext }: { onNext: () => void }) => (
    <button type="button" onClick={onNext}>
      next-database
    </button>
  ),
}));
vi.mock("@/components/setup/security-step", () => ({
  SecurityStep: ({ onNext }: { onNext: () => void }) => (
    <button type="button" onClick={onNext}>
      next-security
    </button>
  ),
}));
vi.mock("@/components/setup/display-step", () => ({
  DisplayStep: ({ onNext }: { onNext: () => void }) => (
    <button type="button" onClick={onNext}>
      next-display
    </button>
  ),
}));
vi.mock("@/components/setup/runner-step", () => ({
  RunnerStep: ({ onNext }: { onNext: () => void }) => (
    <button type="button" onClick={onNext}>
      next-runner
    </button>
  ),
}));
vi.mock("@/components/setup/complete-step", () => ({
  CompleteStep: ({
    onComplete,
    onSignIn,
    result,
  }: {
    onComplete: () => void;
    onSignIn: () => void;
    result: SetupCompleteResponse | null;
  }) =>
    result ? (
      <button type="button" onClick={onSignIn}>
        sign-in
      </button>
    ) : (
      <button type="button" onClick={onComplete}>
        complete-installation
      </button>
    ),
}));

function renderSetupPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <SetupPage />
    </QueryClientProvider>,
  );
  return queryClient;
}

const pendingStatus: SetupStatus = {
  setup_required: true,
  wizard_enabled: true,
  installation_complete: false,
  installation_status: "pending",
  setup_error: null,
  deployment_profile: "single_host",
  database_accessible: true,
  runner_connected: false,
};

const completeResponse: SetupCompleteResponse = {
  status: "success",
  message: "Setup completed successfully",
  redirect: "/auth",
  admin_username: "admin",
  runner_site_created: true,
  runner_enrollment_published: true,
  runner_readiness: "waiting_for_runner",
  runtime_services_started: true,
  restart_required: false,
};

describe("<SetupPage />", () => {
  beforeEach(() => {
    mocked.fetchSetupStatus.mockResolvedValue(pendingStatus);
    mocked.completeSetup.mockResolvedValue(completeResponse);
    mocked.skipSetupWizard.mockResolvedValue(completeResponse);
    setLocation.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("marks setup status complete in cache before navigating to sign-in", async () => {
    const queryClient = renderSetupPage();

    for (const label of ["next-welcome", "next-database", "next-security", "next-display", "next-runner"]) {
      fireEvent.click(await screen.findByText(label));
    }
    fireEvent.click(await screen.findByText("complete-installation"));

    await waitFor(() => expect(mocked.completeSetup).toHaveBeenCalledOnce());
    await waitFor(() => {
      expect(queryClient.getQueryData<SetupStatus>(["/api/setup/status"])).toMatchObject({
        setup_required: false,
        installation_complete: true,
        installation_status: "complete",
      });
    });

    fireEvent.click(await screen.findByText("sign-in"));

    expect(setLocation).toHaveBeenCalledWith("/auth");
  });
});
