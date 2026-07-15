/**
 * Verifies real system metrics and existing task data in the system settings panel.
 */
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SystemSettingsPanel } from "@/components/settings/system-settings-panel";

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        queryFn: ({ queryKey }) => {
          const endpoint = String(queryKey[0]);
          if (endpoint === "/api/settings/system/metrics") {
            return Promise.resolve({
              memory: {
                total_bytes: 16 * 1024 ** 3,
                used_bytes: 6 * 1024 ** 3,
                available_bytes: 10 * 1024 ** 3,
                usage_percent: 37.5,
              },
              storage: {
                total_bytes: 100 * 1024 ** 3,
                used_bytes: 15.2 * 1024 ** 3,
                available_bytes: 84.8 * 1024 ** 3,
                usage_percent: 15.2,
              },
              uptime_seconds: 7 * 24 * 60 * 60 + 12 * 60 * 60,
              collected_at: "2026-07-10T12:00:00Z",
            });
          }
          if (endpoint === "/api/tasks/") {
            return Promise.resolve([
              { id: 1, status: "running" },
              { id: 2, status: "completed" },
              { id: 3, status: "RUNNING" },
            ]);
          }
          throw new Error(`Unexpected query endpoint: ${endpoint}`);
        },
      },
    },
  });

  return render(
    <QueryClientProvider client={client}>
      <SystemSettingsPanel />
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe("<SystemSettingsPanel />", () => {
  it("renders polled host metrics and counts running tasks from the shared task query", async () => {
    renderPanel();

    expect(await screen.findByText("6 GB")).toBeTruthy();
    expect(screen.getByText("15.2 GB")).toBeTruthy();
    expect(screen.getByText("7d 12h")).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
    expect(screen.getByText("of 16 GB memory")).toBeTruthy();
    expect(screen.getByText("of 100 GB workspace storage")).toBeTruthy();
  });

  it("does not render the removed danger zone", async () => {
    renderPanel();

    expect(await screen.findByText("System overview")).toBeTruthy();
    expect(screen.queryByText("Danger Zone")).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear Data" })).toBeNull();
  });
});
