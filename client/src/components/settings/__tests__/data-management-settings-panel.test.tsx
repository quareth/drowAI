// @vitest-environment jsdom
/* Tests for tenant data management settings UI. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DataManagementSettingsPanel } from "@/components/settings/data-management-settings-panel";
import { DATA_MANAGEMENT_SETTINGS_QUERY_KEY } from "@/hooks/use-data-management-settings";

const mocked = vi.hoisted(() => ({
  apiRequest: vi.fn(),
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: mocked.apiRequest,
}));

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        queryFn: () =>
          Promise.resolve({
            tenant_id: 701,
            report_retention_enabled: true,
            report_history_retention_days: 180,
            created_at: "2026-06-13T10:00:00Z",
            updated_at: "2026-06-13T10:00:00Z",
          }),
      },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <DataManagementSettingsPanel queryEnabled />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  mocked.apiRequest.mockReset();
});

describe("<DataManagementSettingsPanel />", () => {
  it("loads and saves report retention policy", async () => {
    mocked.apiRequest.mockResolvedValue(
      new Response(
        JSON.stringify({
          tenant_id: 701,
          report_retention_enabled: true,
          report_history_retention_days: 90,
          created_at: "2026-06-13T10:00:00Z",
          updated_at: "2026-06-13T10:05:00Z",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    renderPanel();

    expect(await screen.findByText("Historical report retention days")).toBeTruthy();
    expect(screen.queryByText("Report retention")).toBeNull();
    const input = screen.getByLabelText("Historical report retention days");
    fireEvent.change(input, { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: /Save policy/i }));

    await waitFor(() =>
      expect(mocked.apiRequest).toHaveBeenCalledWith(
        "PUT",
        "/api/settings/data-management",
        {
          report_history_retention_days: 90,
        },
      ),
    );
    expect(DATA_MANAGEMENT_SETTINGS_QUERY_KEY[0]).toBe(
      "/api/settings/data-management",
    );
  });
});
