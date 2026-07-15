/**
 * Regression coverage for setup wizard product-facing copy.
 */
// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DatabaseStep } from "@/components/setup/database-step";
import { NetworkingStep } from "@/components/setup/networking-step";
import { SETUP_STEPS } from "@/components/setup/setup-types";
import { WelcomeStep } from "@/components/setup/welcome-step";

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

describe("setup wizard copy", () => {
  afterEach(() => {
    cleanup();
  });

  it("uses Runner-focused wording on the welcome step", () => {
    render(<WelcomeStep onNext={vi.fn()} onSkip={vi.fn()} skipLoading={false} />);

    expect(screen.getByText("Default Runner Site for task runtime readiness.")).toBeTruthy();
    expect(screen.getByText("Provision the default Runner Site")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(new RegExp(["execution", "site"].join(" "), "i"));
  });

  it("does not describe database setup as container host plumbing", () => {
    renderWithQueryClient(
      <DatabaseStep
        config={{ db_name: "drowai", db_user: "drowai_user", db_password: "password-123" }}
        onUpdate={vi.fn()}
        onNext={vi.fn()}
        onPrevious={vi.fn()}
      />,
    );

    expect(screen.getByText("The setup process writes these credentials into the generated deployment config.")).toBeTruthy();
    expect(screen.getByText(/These credentials will be used by the deployment database service/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(new RegExp(["Docker", "environment"].join(" "), "i"));
    expect(document.body.textContent).not.toMatch(new RegExp(["managed", "Docker"].join(" "), "i"));
  });

  it("labels reserved runtime networking without Docker primary wording", () => {
    render(
      <NetworkingStep
        config={{ kali_docker_network: "" }}
        onUpdate={vi.fn()}
        onNext={vi.fn()}
        onPrevious={vi.fn()}
      />,
    );

    expect(screen.getByText("Kali Runtime Network")).toBeTruthy();
    expect(screen.getByText("Placeholder settings stored for future Runner Site and runtime network readiness.")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(new RegExp(["Kali", "Docker", "Network"].join(" "), "i"));
  });

  it("uses Runner Site readiness in setup step metadata", () => {
    expect(SETUP_STEPS.find((step) => step.title === "Runner")?.description).toBe("Runner Site readiness");
  });
});
