/**
 * Purpose: Validate OverviewShell non-browser layout composition.
 */
// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";

import { OverviewShell } from "@/components/workbench/overview-shell";

let chatMountCount = 0;
let chatUnmountCount = 0;

vi.mock("@/components/panels/task-panel", () => ({
  TaskPanel: () => <div data-testid="task-panel" />,
}));

vi.mock("@/components/panels/terminal-panel", () => ({
  TerminalPanel: () => <div data-testid="terminal-panel" />,
}));

vi.mock("@/components/chat/UnifiedAgentChat", () => ({
  UnifiedAgentChat: () => {
    useEffect(() => {
      chatMountCount += 1;
      return () => {
        chatUnmountCount += 1;
      };
    }, []);

    return <div data-testid="chat-panel" />;
  },
}));

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    effectivePermissions: { actions: ["task.control"] },
  }),
}));

vi.mock("@/state/workbench-state-store", () => ({
  toggleTerminalCollapsed: vi.fn(),
  useWorkbenchStateSnapshot: () => ({ isTerminalCollapsed: true }),
}));

describe("OverviewShell layout modes", () => {
  beforeEach(() => {
    chatMountCount = 0;
    chatUnmountCount = 0;
  });

  afterEach(() => {
    cleanup();
  });

  it("renders default layout as task + chat", () => {
    render(<OverviewShell chatMode="plan" onChatModeChange={vi.fn()} />);

    expect(screen.getByTestId("task-panel")).toBeTruthy();
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
  });

  it("keeps chat mounted across rerenders", () => {
    render(<OverviewShell chatMode="plan" onChatModeChange={vi.fn()} />);
    expect(chatMountCount).toBe(1);
    expect(chatUnmountCount).toBe(0);
    cleanup();
    render(<OverviewShell chatMode="plan" onChatModeChange={vi.fn()} />);
    expect(chatMountCount).toBe(2);
    expect(chatUnmountCount).toBe(1);
  });
});
