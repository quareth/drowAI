/**
 * Profile page regression coverage for app-shell alignment and account access data.
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import {
  type AppNavigate,
  navigateToAccountPage,
} from "@/navigation/account-page-history";
import ProfilePage from "@/pages/profile-page";

vi.mock("@/components/layout/navbar", () => ({
  Navbar: () => <div>navbar-shell</div>,
}));

vi.mock("@/components/layout/sidebar", () => ({
  Sidebar: () => <div>sidebar-shell</div>,
}));

vi.mock("@/components/password-change-form", () => ({
  PasswordChangeForm: () => <div>password-change-form</div>,
}));

vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({
    user: {
      id: 17,
      username: "garabet",
      email: "garabet@example.test",
      created_at: "2026-01-15T10:00:00Z",
      is_active: true,
    },
  }),
}));

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    activeTenant: {
      tenant_id: 3,
      membership_id: 9,
      role: "tenant_admin",
      is_default_tenant: true,
      source: "token",
    },
    membershipSummaries: [
      {
        membership_id: 9,
        tenant_id: 3,
        tenant_slug: "security-operations",
        tenant_name: "Security Operations",
        role: "tenant_admin",
        membership_status: "active",
        tenant_status: "active",
        is_default_tenant: true,
      },
    ],
    effectivePermissions: {
      actions: ["tasks.read", "tasks.write"],
      role: "tenant_admin",
      tenant_id: 3,
      policy_version: "v1",
    },
  }),
}));

vi.mock("@/hooks/use-user-timezone", () => ({
  useUserTimezone: () => "UTC",
}));

const browserNavigate: AppNavigate = (target, options) => {
  window.history[options?.replace ? "replaceState" : "pushState"](
    options?.state ?? null,
    "",
    target,
  );
};

describe("ProfilePage", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/profile");
  });

  afterEach(() => {
    cleanup();
  });

  it("uses the standard app shell and real account access data", () => {
    render(<ProfilePage />);

    expect(screen.getByText("navbar-shell")).toBeTruthy();
    expect(screen.getByText("sidebar-shell")).toBeTruthy();
    expect(screen.getAllByText("garabet").length).toBeGreaterThan(0);
    expect(screen.getAllByText("garabet@example.test").length).toBeGreaterThan(0);
    expect(screen.getByText(/Tenant Admin/)).toBeTruthy();

    expect(screen.getByRole("tab", { name: /access/i })).toBeTruthy();
    expect(screen.queryByText("Back to Dashboard")).toBeNull();
    expect(screen.queryByText("Achievements")).toBeNull();
  });

  it("switches rendered tab content when a tab updates only the query string", () => {
    render(<ProfilePage />);

    fireEvent.mouseDown(screen.getByRole("tab", { name: /access/i }));

    expect(screen.getByRole("tab", { name: /access/i }).getAttribute("data-state")).toBe("active");
    expect(window.location.pathname).toBe("/profile");
    expect(window.location.search).toBe("?tab=access");
  });

  it("returns to the exact in-app origin after switching Profile tabs", async () => {
    window.history.replaceState(null, "", "/reports?tab=library&engagement_id=7");
    navigateToAccountPage(browserNavigate, "/profile", "/reports");
    render(<ProfilePage />);

    fireEvent.mouseDown(screen.getByRole("tab", { name: /access/i }));
    expect(window.location.pathname).toBe("/profile");
    expect(window.location.search).toBe("?tab=access");

    fireEvent.click(screen.getByRole("button", { name: "Go back" }));

    await waitFor(() => {
      expect(window.location.pathname).toBe("/reports");
      expect(window.location.search).toBe("?tab=library&engagement_id=7");
    });
  });

  it("uses the app dashboard fallback for direct Profile entry", () => {
    render(<ProfilePage />);

    fireEvent.click(screen.getByRole("button", { name: "Go back" }));

    expect(window.location.pathname).toBe("/");
    expect(window.location.search).toBe("");
  });
});
