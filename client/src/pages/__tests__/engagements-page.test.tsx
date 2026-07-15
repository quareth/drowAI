// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/layout/sidebar";

const mocked = vi.hoisted(() => ({
  useLocation: vi.fn(),
}));

vi.mock("wouter", () => ({
  Link: ({ href, children }: { href: string; children: React.ReactNode }) => (
    <a href={href}>{children}</a>
  ),
  useLocation: mocked.useLocation,
}));

vi.mock("@/components/layout/navbar", () => ({
  Navbar: () => <div data-testid="navbar">navbar</div>,
}));

describe("knowledge migration (sidebar)", () => {
  afterEach(() => {
    cleanup();
    mocked.useLocation.mockReset();
  });

  it("highlights Knowledge nav for /knowledge route", () => {
    mocked.useLocation.mockReturnValue(["/knowledge", vi.fn()]);

    render(<Sidebar />);

    const knowledgeButton = screen.getAllByRole("button", { name: "Knowledge" })[0];
    const dashboardButton = screen.getAllByRole("button", { name: "Outpost" })[0];

    expect(knowledgeButton.className).toContain("bg-blue-600");
    expect(dashboardButton.className).not.toContain("bg-blue-600");
  });

  it("does not render Live Agent navigation entry", () => {
    mocked.useLocation.mockReturnValue(["/knowledge", vi.fn()]);

    render(<Sidebar />);

    expect(screen.queryByRole("button", { name: "Live Agent" })).toBeNull();
  });

  it("does not render Settings in the primary drawer navigation", () => {
    mocked.useLocation.mockReturnValue(["/settings", vi.fn()]);

    render(<Sidebar />);

    expect(screen.queryByRole("button", { name: "Settings" })).toBeNull();
  });
});
