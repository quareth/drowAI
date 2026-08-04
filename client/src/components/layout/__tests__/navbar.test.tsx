// @vitest-environment jsdom
/**
 * Navbar account-menu regression coverage for profile-scoped navigation.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Navbar } from "@/components/layout/navbar";
import { hasInAppAccountPageOrigin } from "@/navigation/account-page-history";

const mocked = vi.hoisted(() => ({
  location: "/profile",
  setLocation: vi.fn(),
  logout: vi.fn(),
  tenantContext: {
    activeTenant: null as null | { tenant_id: number },
    isMultiTenant: false,
    isSwitchingTenant: false,
    membershipSummaries: [] as Array<{
      membership_id: number;
      tenant_id: number;
      tenant_name: string;
    }>,
    switchTenant: vi.fn(),
  },
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
  useTenantContext: () => mocked.tenantContext,
}));

vi.mock("@/components/layout/notification-menu", () => ({
  NotificationMenu: () => <div data-testid="notification-menu" />,
}));

describe("<Navbar />", () => {
  afterEach(() => {
    cleanup();
    mocked.setLocation.mockReset();
    mocked.logout.mockReset();
    mocked.location = "/profile";
    mocked.tenantContext.activeTenant = null;
    mocked.tenantContext.isMultiTenant = false;
    mocked.tenantContext.isSwitchingTenant = false;
    mocked.tenantContext.membershipSummaries = [];
    mocked.tenantContext.switchTenant.mockReset();
  });

  it.each([
    "/?workspace=files",
    "/knowledge?tab=assets",
    "/reports?tab=library&engagement_id=7",
    "/usage",
    "/profile?tab=security",
  ])("marks Settings navigation from %s as having an in-app origin", (origin) => {
    mocked.location = origin;
    render(<Navbar />);

    fireEvent.pointerDown(screen.getByRole("button", { name: /alice/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: /settings/i }));

    expect(mocked.setLocation).toHaveBeenCalledWith(
      "/settings",
      expect.objectContaining({ replace: false }),
    );
    const options = mocked.setLocation.mock.calls[0]?.[1] as { state?: unknown };
    expect(hasInAppAccountPageOrigin(options.state)).toBe(true);
  });

  it.each([
    "/?workspace=files",
    "/knowledge?tab=assets",
    "/reports?tab=library&engagement_id=7",
    "/usage",
    "/settings?section=display",
  ])("marks Profile navigation from %s as having an in-app origin", (origin) => {
    mocked.location = origin;
    render(<Navbar />);

    fireEvent.pointerDown(screen.getByRole("button", { name: /alice/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: /^profile$/i }));

    expect(mocked.setLocation).toHaveBeenCalledWith(
      "/profile",
      expect.objectContaining({ replace: false }),
    );
    const options = mocked.setLocation.mock.calls[0]?.[1] as { state?: unknown };
    expect(hasInAppAccountPageOrigin(options.state)).toBe(true);
  });

  it("gives the tenant selector an accessible name", () => {
    mocked.tenantContext.activeTenant = { tenant_id: 11 };
    mocked.tenantContext.isMultiTenant = true;
    mocked.tenantContext.membershipSummaries = [
      { membership_id: 1, tenant_id: 11, tenant_name: "Tenant A" },
    ];

    render(<Navbar />);

    expect(screen.getByRole("combobox", { name: "Tenant" })).toBeTruthy();
  });
});
