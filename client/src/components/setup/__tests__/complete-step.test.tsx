/**
 * Regression coverage for setup completion readiness presentation.
 */
// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CompleteStep } from "@/components/setup/complete-step";
import type { SetupCompleteResponse, SetupConfig } from "@/components/setup/setup-types";

const config: SetupConfig = {
  database: {
    db_name: "drowai",
    db_user: "drowai_user",
    db_password: "password-123",
  },
  security: {
    session_timeout: 30,
    admin_username: "admin",
    admin_email: "admin@example.test",
    admin_password: "password-123",
  },
  display: {
    timezone: "UTC",
  },
  network: {},
  runner: {
    create_site: true,
    site_name: "Default Site",
    site_slug: "default-site",
  },
};

function renderCompleteStep(result: SetupCompleteResponse) {
  render(
    <CompleteStep
      config={config}
      onComplete={vi.fn()}
      onPrevious={vi.fn()}
      isLoading={false}
      error={null}
      result={result}
      onSignIn={vi.fn()}
    />,
  );
}

describe("CompleteStep", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders waiting runtime readiness without raw enrollment mechanics", () => {
    renderCompleteStep({
      status: "success",
      message: "Setup completed successfully",
      redirect: "/auth",
      admin_username: "admin",
      runner_site_created: true,
      runner_enrollment_published: true,
      runner_readiness: "waiting_for_runner",
      runtime_services_started: true,
      restart_required: false,
    });

    expect(screen.getByText("Setup complete")).toBeTruthy();
    expect(screen.getByText("Local Runner enrollment was published for this Runner Site.")).toBeTruthy();
    expect(screen.getByText("Waiting for a Runner connection before task runtime work can start.")).toBeTruthy();
    expect(screen.queryByText(/install token/i)).toBeNull();
    expect(screen.queryByText(/registration token/i)).toBeNull();
    expect(screen.queryByText(/tenant id/i)).toBeNull();
    expect(screen.queryByText(/execution site/i)).toBeNull();
    expect(screen.queryByText(/enrollment id/i)).toBeNull();
    expect(screen.queryByText(/runner id/i)).toBeNull();
  });

  it("renders connected runtime readiness distinctly from waiting state", () => {
    renderCompleteStep({
      status: "success",
      message: "Setup completed successfully",
      redirect: "/auth",
      admin_username: "admin",
      runner_site_created: true,
      runner_enrollment_published: true,
      runner_readiness: "ready",
      runtime_services_started: true,
      restart_required: false,
    });

    expect(screen.getByText("A Runner is connected and ready for task runtime work.")).toBeTruthy();
    expect(screen.getByText("Sign in and create tasks through the connected Runner.")).toBeTruthy();
  });
});
