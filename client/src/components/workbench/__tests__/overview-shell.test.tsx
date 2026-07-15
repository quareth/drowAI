/**
 * Purpose: Validate OverviewShell baseline behavior without browser controls.
 */
// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OverviewShell } from "@/components/workbench/overview-shell";

const toggleTerminalCollapsedMock = vi.fn();
let mockedActions: string[] = ["task.control"];

vi.mock("@/components/panels/task-panel", () => ({
  TaskPanel: () => <div data-testid="task-panel" />,
}));

vi.mock("@/components/panels/terminal-panel", () => ({
  TerminalPanel: () => <div data-testid="terminal-panel" />,
}));

vi.mock("@/components/chat/UnifiedAgentChat", () => ({
  UnifiedAgentChat: () => <div data-testid="chat-panel" />,
}));

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    effectivePermissions: { actions: mockedActions },
  }),
}));

vi.mock("@/state/workbench-state-store", () => ({
  toggleTerminalCollapsed: () => toggleTerminalCollapsedMock(),
  useWorkbenchStateSnapshot: () => ({ isTerminalCollapsed: true }),
}));

describe("OverviewShell baseline", () => {
  beforeEach(() => {
    toggleTerminalCollapsedMock.mockClear();
    mockedActions = ["task.control"];
  });

  afterEach(() => {
    cleanup();
  });

  it("renders task + chat panes without browser control", () => {
    render(<OverviewShell chatMode="plan" onChatModeChange={vi.fn()} />);
    expect(screen.getByTestId("task-panel")).toBeTruthy();
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
    expect(screen.getByTestId("terminal-panel")).toBeTruthy();
    expect(screen.queryByLabelText("Open browser mode")).toBeNull();
  });

  it("hides terminal dock when tenant actions do not include task.control", () => {
    mockedActions = ["task.read"];
    render(<OverviewShell chatMode="plan" onChatModeChange={vi.fn()} />);
    expect(screen.queryByTestId("terminal-panel")).toBeNull();
  });
});
