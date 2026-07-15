/**
 * Navbar search behavior tests for app destination navigation.
 */
// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Navbar } from "@/components/layout/navbar";

const mocked = vi.hoisted(() => ({
  setLocation: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("wouter", () => ({
  useLocation: () => ["/", mocked.setLocation],
}));

vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({
    user: {
      id: 7,
      username: "alice",
      email: "alice@example.test",
      created_at: "2026-01-01T00:00:00Z",
      is_active: true,
    },
    logoutMutation: {
      mutate: mocked.logout,
    },
  }),
}));

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    activeTenant: null,
    effectivePermissions: { actions: [] },
    isMultiTenant: false,
    isSwitchingTenant: false,
    membershipSummaries: [],
    switchTenant: vi.fn(),
  }),
}));

vi.mock("@/components/layout/notification-menu", () => ({
  NotificationMenu: () => <div data-testid="notification-menu" />,
}));

describe("<Navbar /> destination search", () => {
  afterEach(() => {
    cleanup();
    mocked.setLocation.mockReset();
    mocked.logout.mockReset();
  });

  it("opens matching destinations and navigates on click", () => {
    render(<Navbar />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "api" } });
    fireEvent.click(screen.getByRole("option", { name: /api settings/i }));

    expect(mocked.setLocation).toHaveBeenCalledWith("/settings?section=api");
  });

  it("navigates to the active result on Enter", () => {
    render(<Navbar />);

    const search = screen.getByRole("combobox");
    fireEvent.change(search, { target: { value: "display" } });
    fireEvent.keyDown(search, { key: "Enter" });

    expect(mocked.setLocation).toHaveBeenCalledWith("/settings?section=display");
  });

  it("closes results on Escape", () => {
    render(<Navbar />);

    const search = screen.getByRole("combobox");
    fireEvent.change(search, { target: { value: "report" } });
    expect(screen.getByRole("listbox")).toBeTruthy();

    fireEvent.keyDown(search, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
