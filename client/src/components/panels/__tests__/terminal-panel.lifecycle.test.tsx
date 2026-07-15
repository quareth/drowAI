/**
 * Purpose: Validate terminal dock delete/restore lifecycle.
 */
// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TerminalPanel } from "@/components/panels/terminal-panel";
import { readTerminalList, writeTerminalList } from "@/lib/terminal-storage";

const closeSessionMock = vi.fn();
const ensureConnectionMock = vi.fn();

vi.mock("@/hooks/useTerminalSockets", () => ({
  useTerminalSockets: () => ({
    ensureConnection: ensureConnectionMock,
    getWebSocket: () => undefined,
    getSessionId: () => null,
    sendInput: vi.fn(),
    close: vi.fn(),
    closeSession: closeSessionMock,
  }),
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => ({
    isSuccess: true,
    data: [
      { id: 7, name: "Task Seven", status: "running" },
      { id: 8, name: "Task Eight", status: "running" },
    ],
  }),
}));

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    effectivePermissions: { actions: ["task.control"] },
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/lib/queryClient", () => ({
  queryClient: {
    invalidateQueries: vi.fn(),
  },
}));

vi.mock("@/state/workbench-state-store", () => ({
  useWorkbenchStateSnapshot: () => ({
    terminalTaskId: null,
    terminalRequestNonce: 0,
  }),
}));

vi.mock("xterm", () => ({
  Terminal: function MockTerminal() {
    return {
      cols: 80,
      rows: 24,
      loadAddon: vi.fn(),
      open: vi.fn(),
      focus: vi.fn(),
      reset: vi.fn(),
      write: vi.fn(),
      dispose: vi.fn(),
      onData: vi.fn(() => ({ dispose: vi.fn() })),
    };
  },
}));

vi.mock("xterm-addon-fit", () => ({
  FitAddon: function MockFitAddon() {
    return {
      fit: vi.fn(),
    };
  },
}));

describe("TerminalPanel lifecycle", () => {
  beforeEach(() => {
    sessionStorage.clear();
    closeSessionMock.mockClear();
    ensureConnectionMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("persists terminal deletion before remount so closed tabs do not restore", async () => {
    writeTerminalList([
      { id: "terminal-7", taskId: 7, taskName: "Task Seven", isActive: true },
      { id: "terminal-8", taskId: 8, taskName: "Task Eight", isActive: false },
    ]);
    sessionStorage.setItem("termsid:7", "sid-7");
    sessionStorage.setItem("termbuf:7", "old output");

    const view = render(<TerminalPanel isCollapsed={false} onToggleCollapse={vi.fn()} />);
    fireEvent.click(screen.getAllByTitle("Close terminal")[0]);

    expect(closeSessionMock).toHaveBeenCalledWith("terminal-7");
    expect(readTerminalList()).toEqual([
      { id: "terminal-8", taskId: 8, taskName: "Task Eight", isActive: true },
    ]);
    expect(sessionStorage.getItem("termsid:7")).toBeNull();
    expect(sessionStorage.getItem("termbuf:7")).toBeNull();

    view.unmount();
    render(<TerminalPanel isCollapsed={false} onToggleCollapse={vi.fn()} />);

    expect(screen.queryByText("Task 7")).toBeNull();
    expect(screen.getByText("Task 8")).toBeTruthy();
  });

  it("deleting the last terminal clears the persisted list and viewport state", () => {
    writeTerminalList([{ id: "terminal-7", taskId: 7, taskName: "Task Seven", isActive: true }]);
    sessionStorage.setItem("termsid:7", "sid-7");
    sessionStorage.setItem("termbuf:7", "old output");

    render(<TerminalPanel isCollapsed={false} onToggleCollapse={vi.fn()} />);
    fireEvent.click(screen.getByTitle("Close terminal"));

    expect(readTerminalList()).toEqual([]);
    expect(sessionStorage.getItem("term:list")).toBeNull();
    expect(sessionStorage.getItem("termsid:7")).toBeNull();
    expect(sessionStorage.getItem("termbuf:7")).toBeNull();
    expect(screen.getByText("No terminals")).toBeTruthy();
  });

  it("does not expose the unused More Actions placeholder", () => {
    render(<TerminalPanel isCollapsed={false} onToggleCollapse={vi.fn()} />);

    expect(screen.queryByTitle("More Actions")).toBeNull();
  });
});
