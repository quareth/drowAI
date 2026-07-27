/**
 * Settings page query-param navigation tests.
 */
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type AppNavigate,
  navigateToAccountPage,
} from "@/navigation/account-page-history";
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

const browserNavigate: AppNavigate = (target, options) => {
  window.history[options?.replace ? "replaceState" : "pushState"](
    options?.state ?? null,
    "",
    target,
  );
};

describe("SettingsPage navigation", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/settings");
  });

  afterEach(() => {
    cleanup();
  });

  it("opens Display from the section query parameter", () => {
    window.history.replaceState(null, "", "/settings?section=display");

    renderPage();

    expect(screen.getByRole("tab", { name: /display/i }).getAttribute("data-state")).toBe("active");
    expect(screen.getByTestId("display-settings-panel")).toBeTruthy();
  });

  it("falls back to API for invalid sections", () => {
    window.history.replaceState(null, "", "/settings?section=unknown");

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

  it("returns to the exact in-app origin after switching Settings sections", async () => {
    window.history.replaceState(null, "", "/reports?tab=library&engagement_id=7");
    navigateToAccountPage(browserNavigate, "/settings", "/reports");
    renderPage();

    fireEvent.mouseDown(screen.getByRole("tab", { name: /display/i }));
    expect(window.location.pathname).toBe("/settings");
    expect(window.location.search).toBe("?section=display");

    fireEvent.click(screen.getByRole("button", { name: "Go back" }));

    await waitFor(() => {
      expect(window.location.pathname).toBe("/reports");
      expect(window.location.search).toBe("?tab=library&engagement_id=7");
    });
  });

  it("uses the app dashboard fallback for direct Settings entry", () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Go back" }));

    expect(window.location.pathname).toBe("/");
    expect(window.location.search).toBe("");
  });
});
