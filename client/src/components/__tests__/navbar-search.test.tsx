/**
 * Navbar search behavior tests for app destination navigation.
 */
// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Navbar } from "@/components/layout/navbar";
import { hasInAppAccountPageOrigin } from "@/navigation/account-page-history";

const mocked = vi.hoisted(() => ({
  location: "/",
  setLocation: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("wouter", () => ({
  useLocation: () => [mocked.location, mocked.setLocation],
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
    mocked.location = "/";
    window.history.replaceState(null, "", "/");
  });

  it("opens matching destinations and navigates on click", () => {
    render(<Navbar />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "api" } });
    fireEvent.click(screen.getByRole("option", { name: /api settings/i }));

    expect(mocked.setLocation).toHaveBeenCalledWith(
      "/settings?section=api",
      expect.objectContaining({ replace: false }),
    );
    const options = mocked.setLocation.mock.calls[0]?.[1] as { state?: unknown };
    expect(hasInAppAccountPageOrigin(options.state)).toBe(true);
  });

  it("navigates to the active result on Enter", () => {
    render(<Navbar />);

    const search = screen.getByRole("combobox");
    fireEvent.change(search, { target: { value: "display" } });
    fireEvent.keyDown(search, { key: "Enter" });

    expect(mocked.setLocation).toHaveBeenCalledWith(
      "/settings?section=display",
      expect.objectContaining({ replace: false }),
    );
    const options = mocked.setLocation.mock.calls[0]?.[1] as { state?: unknown };
    expect(hasInAppAccountPageOrigin(options.state)).toBe(true);
  });

  it("replaces Settings-to-Settings search navigation without losing history state", () => {
    const currentState = { existingSettingsOrigin: true };
    mocked.location = "/settings";
    window.history.replaceState(currentState, "", "/settings");
    render(<Navbar />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "display" } });
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });

    expect(mocked.setLocation).toHaveBeenCalledWith("/settings?section=display", {
      replace: true,
      state: currentState,
    });
  });

  it("marks Profile search navigation as having an in-app origin", () => {
    render(<Navbar />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "profile security" } });
    fireEvent.click(screen.getByRole("option", { name: /profile security/i }));

    expect(mocked.setLocation).toHaveBeenCalledWith(
      "/profile?tab=security",
      expect.objectContaining({ replace: false }),
    );
    const options = mocked.setLocation.mock.calls[0]?.[1] as { state?: unknown };
    expect(hasInAppAccountPageOrigin(options.state)).toBe(true);
  });

  it("replaces Profile-to-Profile search navigation without losing history state", () => {
    const currentState = { existingProfileOrigin: true };
    mocked.location = "/profile";
    window.history.replaceState(currentState, "", "/profile");
    render(<Navbar />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "profile security" } });
    fireEvent.click(screen.getByRole("option", { name: /profile security/i }));

    expect(mocked.setLocation).toHaveBeenCalledWith("/profile?tab=security", {
      replace: true,
      state: currentState,
    });
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
