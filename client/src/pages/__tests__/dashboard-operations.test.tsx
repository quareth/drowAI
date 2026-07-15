// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Dashboard from "@/pages/dashboard";

vi.mock("@/components/layout/navbar", () => ({
  Navbar: () => <div data-testid="navbar">navbar</div>,
}));

vi.mock("@/components/layout/sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar">sidebar</div>,
}));

vi.mock("@/components/workbench/overview-shell", () => ({
  OverviewShell: () => <div data-testid="overview-shell">overview-shell</div>,
}));

vi.mock("@/components/panels/file-explorer-panel", () => ({
  FileExplorerPanel: () => <div data-testid="file-explorer-panel">file-explorer-panel</div>,
}));

vi.mock("@/components/panels/file-preview-panel", () => ({
  FilePreviewPanel: () => <div data-testid="file-preview-panel">file-preview-panel</div>,
}));

vi.mock("@/components/panels/threat-dashboard-panel", () => ({
  ThreatDashboardPanel: () => <div data-testid="threat-dashboard-panel">threat-dashboard-panel</div>,
}));

vi.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  ResizablePanel: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  ResizableHandle: () => <div />,
}));

describe("dashboard workspace navigation", () => {
  beforeEach(() => {
    window.history.pushState({}, "", "/");
  });

  afterEach(() => {
    cleanup();
  });

  it("renders only Operations, File Explorer, and Threat Dashboard tabs", () => {
    render(<Dashboard />);

    expect(screen.getByRole("button", { name: "Operations" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "File Explorer" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Threat Dashboard" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Findings" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Map" })).toBeNull();
  });

  it("switches between the remaining dashboard workspaces", () => {
    render(<Dashboard />);

    expect(screen.getByTestId("overview-shell")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "File Explorer" }));
    expect(screen.getByTestId("file-explorer-panel")).toBeTruthy();
    expect(screen.getByTestId("file-preview-panel")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Threat Dashboard" }));
    expect(screen.getByTestId("threat-dashboard-panel")).toBeTruthy();
  });

  it("opens the workspace from the query parameter", () => {
    window.history.pushState({}, "", "/?workspace=files");

    render(<Dashboard />);

    expect(screen.getByTestId("file-explorer-panel")).toBeTruthy();
    expect(screen.getByTestId("file-preview-panel")).toBeTruthy();
  });
});
