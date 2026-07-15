/**
 * Engagement combobox default behavior tests.
 *
 * Responsibility: protect the task-creation defaults while reporting passes
 * stricter optional props for concrete engagement selection.
 */
// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EngagementCombobox } from "@/components/engagements/engagement-combobox";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  apiRequest: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: mocked.apiRequest,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocked.toast }),
}));

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function renderCombobox() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  render(
    <QueryClientProvider client={client}>
      <EngagementCombobox value={null} onChange={vi.fn()} />
    </QueryClientProvider>,
  );
}

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

describe("<EngagementCombobox />", () => {
  beforeEach(() => {
    globalThis.ResizeObserver = TestResizeObserver;
    Element.prototype.scrollIntoView = vi.fn();
    mocked.apiFetch.mockResolvedValue(
      jsonResponse({
        items: [
          {
            id: 7,
            user_id: 1,
            name: "Existing Engagement",
            description: null,
            status: "active",
            metadata: {},
            created_at: null,
            updated_at: null,
          },
        ],
        total: 1,
        limit: 100,
        offset: 0,
      }),
    );
  });

  afterEach(() => {
    cleanup();
    mocked.apiFetch.mockReset();
    mocked.apiRequest.mockReset();
    mocked.toast.mockReset();
  });

  it("keeps create and none actions enabled by default", async () => {
    renderCombobox();

    fireEvent.click(screen.getByRole("combobox"));

    expect(await screen.findByText("Existing Engagement")).toBeTruthy();
    expect(screen.getByText("None (auto-create from task name)")).toBeTruthy();
    expect(screen.getByText("Leave empty to auto-create from task name")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("Search engagements…"), {
      target: { value: "New Client Engagement" },
    });

    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: /\+ Create "New Client Engagement"/ }),
      ).toBeTruthy(),
    );
  });
});
