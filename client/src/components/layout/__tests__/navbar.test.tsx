// @vitest-environment jsdom
/**
 * Navbar account-menu regression coverage for profile-scoped navigation.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Navbar } from "@/components/layout/navbar";

const mocked = vi.hoisted(() => ({
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
  useLocation: () => ["/profile", mocked.setLocation],
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
    mocked.tenantContext.activeTenant = null;
    mocked.tenantContext.isMultiTenant = false;
    mocked.tenantContext.isSwitchingTenant = false;
    mocked.tenantContext.membershipSummaries = [];
    mocked.tenantContext.switchTenant.mockReset();
  });

  it("keeps Settings accessible from the account menu", () => {
    render(<Navbar />);

    fireEvent.pointerDown(screen.getByRole("button", { name: /alice/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: /settings/i }));

    expect(mocked.setLocation).toHaveBeenCalledWith("/settings");
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
