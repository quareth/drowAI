/**
 * Verifies the Overview task panel can collapse from its toolbar or resize
 * threshold and reopen without remounting the conversation.
 */
// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OverviewShell } from "@/components/workbench/overview-shell";

let chatMountCount = 0;

vi.mock("@/components/panels/task-panel", () => ({
  TaskPanel: ({ onToggleVisibility }: { onToggleVisibility?: () => void }) => (
    <div data-testid="task-panel">
      <button type="button" onClick={onToggleVisibility} aria-label="Close task panel">
        Close
      </button>
    </div>
  ),
}));

vi.mock("@/components/panels/terminal-panel", () => ({
  TerminalPanel: () => <div data-testid="terminal-panel" />,
}));

vi.mock("@/components/chat/UnifiedAgentChat", async () => {
  const React = await import("react");
  return {
    UnifiedAgentChat: ({ leadingHeaderSlot }: { leadingHeaderSlot?: React.ReactNode }) => {
      React.useEffect(() => {
        chatMountCount += 1;
      }, []);
      return (
        <div data-testid="chat-panel">
          {leadingHeaderSlot}
        </div>
      );
    },
  };
});

vi.mock("@/components/ui/resizable", async () => {
  const React = await import("react");

  const ResizablePanel = React.forwardRef<
    {
      collapse: () => void;
      expand: () => void;
      isCollapsed: () => boolean;
    },
    {
      children?: React.ReactNode;
      id?: string;
      defaultSize?: number;
      minSize?: number;
      maxSize?: number;
      collapsible?: boolean;
      collapsedSize?: number;
      onCollapse?: () => void;
      onExpand?: () => void;
    }
  >(function MockResizablePanel(
    {
      children,
      id,
      defaultSize,
      minSize,
      maxSize,
      collapsible,
      collapsedSize,
      onCollapse,
      onExpand,
    },
    ref,
  ) {
    const [collapsed, setCollapsed] = React.useState(false);
    const collapse = () => {
      setCollapsed(true);
      onCollapse?.();
    };
    const expand = () => {
      setCollapsed(false);
      onExpand?.();
    };

    React.useImperativeHandle(
      ref,
      () => ({
        collapse,
        expand,
        isCollapsed: () => collapsed,
      }),
      [collapsed],
    );

    return (
      <div
        data-testid={`resizable-panel-${id}`}
        data-default-size={defaultSize}
        data-min-size={minSize}
        data-max-size={maxSize}
        data-collapsible={collapsible}
        data-collapsed-size={collapsedSize}
      >
        {collapsed ? null : children}
        {id === "task-panel" && !collapsed ? (
          <button type="button" onClick={collapse} data-testid="collapse-task-panel">
            Collapse by resize
          </button>
        ) : null}
      </div>
    );
  });

  return {
    ResizablePanel,
    ResizablePanelGroup: ({ children }: { children?: React.ReactNode }) => (
      <div data-testid="resizable-panel-group">{children}</div>
    ),
    ResizableHandle: (props: React.HTMLAttributes<HTMLDivElement>) => (
      <div {...props} data-testid="resizable-panel-handle" />
    ),
  };
});

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    effectivePermissions: { actions: ["task.control"] },
  }),
}));

vi.mock("@/state/workbench-state-store", () => ({
  toggleTerminalCollapsed: vi.fn(),
  useWorkbenchStateSnapshot: () => ({ isTerminalCollapsed: true }),
}));

describe("OverviewShell task panel", () => {
  beforeEach(() => {
    chatMountCount = 0;
  });

  afterEach(() => {
    cleanup();
  });

  it("toggles the task panel while preserving the conversation", async () => {
    render(<OverviewShell chatMode="plan" onChatModeChange={vi.fn()} />);

    const panel = screen.getByTestId("resizable-panel-task-panel");
    expect(panel.getAttribute("data-default-size")).toBe("40");
    expect(panel.getAttribute("data-min-size")).toBe("30");
    expect(panel.getAttribute("data-max-size")).toBe("40");
    expect(panel.getAttribute("data-collapsible")).toBe("true");
    expect(panel.getAttribute("data-collapsed-size")).toBe("0");
    expect(screen.getByLabelText("Resize task panel")).toBeTruthy();
    expect(chatMountCount).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Close task panel" }));

    expect(await screen.findByRole("button", { name: "Open task panel" })).toBeTruthy();
    expect(screen.queryByTestId("task-panel")).toBeNull();
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
    expect(chatMountCount).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Open task panel" }));

    await waitFor(() => {
      expect(screen.getByTestId("task-panel")).toBeTruthy();
    });
    expect(screen.queryByRole("button", { name: "Open task panel" })).toBeNull();
    expect(chatMountCount).toBe(1);
  });

  it("closes when resizing crosses the collapse threshold", async () => {
    render(<OverviewShell chatMode="plan" onChatModeChange={vi.fn()} />);

    fireEvent.click(screen.getByTestId("collapse-task-panel"));

    expect(await screen.findByRole("button", { name: "Open task panel" })).toBeTruthy();
    expect(screen.queryByTestId("task-panel")).toBeNull();
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
  });
});
