/**
 * Settings page query-param navigation tests.
 */
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SettingsPage from "@/pages/settings-page";

vi.mock("@/components/layout/navbar", () => ({
  Navbar: () => <div data-testid="navbar" />,
}));

vi.mock("@/components/layout/sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar" />,
}));

vi.mock("@/components/settings/api-settings-panel", () => ({
  ApiSettingsPanel: () => <div data-testid="api-settings-panel" />,
}));

vi.mock("@/components/settings/network-settings-panel", () => ({
  NetworkSettingsPanel: () => <div data-testid="network-settings-panel" />,
}));

vi.mock("@/components/settings/system-settings-panel", () => ({
  SystemSettingsPanel: () => <div data-testid="system-settings-panel" />,
}));

vi.mock("@/components/settings/data-management-settings-panel", () => ({
  DataManagementSettingsPanel: () => <div data-testid="data-management-settings-panel" />,
}));

vi.mock("@/components/settings/display-settings-panel", () => ({
  DisplaySettingsPanel: () => <div data-testid="display-settings-panel" />,
}));

vi.mock("@/components/settings/cve-settings-panel", () => ({
  CveSettingsPanel: () => <div data-testid="cve-settings-panel" />,
}));

vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({
    user: {
      id: 1,
      username: "alice",
      created_at: "2026-01-01T00:00:00Z",
      is_active: true,
    },
  }),
}));

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

describe("SettingsPage deep links", () => {
  beforeEach(() => {
    window.history.pushState({}, "", "/settings");
  });

  afterEach(() => {
    cleanup();
  });

  it("opens Display from the section query parameter", () => {
    window.history.pushState({}, "", "/settings?section=display");

    renderPage();

    expect(screen.getByRole("tab", { name: /display/i }).getAttribute("data-state")).toBe("active");
    expect(screen.getByTestId("display-settings-panel")).toBeTruthy();
  });

  it("falls back to API for invalid sections", () => {
    window.history.pushState({}, "", "/settings?section=unknown");

    renderPage();

    expect(screen.getByRole("tab", { name: /api/i }).getAttribute("data-state")).toBe("active");
    expect(screen.getByTestId("api-settings-panel")).toBeTruthy();
  });

  it("switches rendered section when a tab updates only the query string", () => {
    renderPage();

    fireEvent.mouseDown(screen.getByRole("tab", { name: /display/i }));

    expect(screen.getByRole("tab", { name: /display/i }).getAttribute("data-state")).toBe("active");
    expect(screen.getByTestId("display-settings-panel")).toBeTruthy();
    expect(window.location.pathname).toBe("/settings");
    expect(window.location.search).toBe("?section=display");
  });
});
