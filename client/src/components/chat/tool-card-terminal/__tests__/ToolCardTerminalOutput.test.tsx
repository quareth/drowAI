// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToolCardTerminalOutput } from "@/components/chat/tool-card-terminal/ToolCardTerminalOutput";

const mocked = vi.hoisted(() => {
  const terminalInstances: Array<{
    open: ReturnType<typeof vi.fn>;
    loadAddon: ReturnType<typeof vi.fn>;
    write: ReturnType<typeof vi.fn>;
    clear: ReturnType<typeof vi.fn>;
    dispose: ReturnType<typeof vi.fn>;
  }> = [];
  const fitAddonInstances: Array<{ fit: ReturnType<typeof vi.fn> }> = [];
  return {
    terminalInstances,
    fitAddonInstances,
    terminalCtor: vi.fn(function terminalCtorMock() {
      const instance = {
        open: vi.fn(),
        loadAddon: vi.fn(),
        write: vi.fn(),
        clear: vi.fn(),
        dispose: vi.fn(),
      };
      terminalInstances.push(instance);
      return instance;
    }),
    fitAddonCtor: vi.fn(function fitAddonCtorMock() {
      const instance = { fit: vi.fn() };
      fitAddonInstances.push(instance);
      return instance;
    }),
  };
});

vi.mock("xterm", () => ({
  Terminal: mocked.terminalCtor,
}));

vi.mock("xterm-addon-fit", () => ({
  FitAddon: mocked.fitAddonCtor,
}));

afterEach(() => {
  cleanup();
  mocked.terminalCtor.mockClear();
  mocked.fitAddonCtor.mockClear();
  mocked.terminalInstances.splice(0, mocked.terminalInstances.length);
  mocked.fitAddonInstances.splice(0, mocked.fitAddonInstances.length);
});

describe("ToolCardTerminalOutput", () => {
  it("lazy-mounts only when expanded and ready", () => {
    const { rerender } = render(
      <ToolCardTerminalOutput outputText="hello" isExpanded={false} isReady={true} testId="tool-terminal" />,
    );

    expect(screen.queryByTestId("tool-terminal")).toBeNull();
    expect(mocked.terminalCtor).not.toHaveBeenCalled();

    rerender(
      <ToolCardTerminalOutput outputText="hello" isExpanded={true} isReady={false} testId="tool-terminal" />,
    );
    expect(screen.queryByTestId("tool-terminal")).toBeNull();
    expect(mocked.terminalCtor).not.toHaveBeenCalled();

    rerender(
      <ToolCardTerminalOutput outputText="hello" isExpanded={true} isReady={true} testId="tool-terminal" />,
    );
    expect(screen.getByTestId("tool-terminal")).not.toBeNull();
    expect(mocked.terminalCtor).toHaveBeenCalledTimes(1);
  });

  it("renders output with fixed-height responsive container", () => {
    render(
      <ToolCardTerminalOutput outputText={"line1\nline2"} isExpanded={true} isReady={true} testId="tool-terminal" />,
    );

    const container = screen.getByTestId("tool-terminal");
    expect(container.className).toContain("h-56");
    expect(container.className).toContain("w-full");
    expect(container.className).toContain("max-w-full");
    expect(container.className).toContain("overflow-hidden");

    const terminal = mocked.terminalInstances[0];
    // Command line (first line) unchanged; output (rest) wrapped in ANSI dim
    expect(terminal.write).toHaveBeenCalledWith(
      "line1\n\x1b[38;5;246mline2\x1b[0m",
    );
    expect(terminal.write).toHaveBeenCalledTimes(1);
    expect(terminal.clear).toHaveBeenCalledTimes(1);
  });

  it("writes once on mount and once per output update", () => {
    const { rerender } = render(
      <ToolCardTerminalOutput outputText="first" isExpanded={true} isReady={true} testId="tool-terminal" />,
    );

    const terminal = mocked.terminalInstances[0];
    expect(terminal.write).toHaveBeenCalledTimes(1);
    expect(terminal.write).toHaveBeenLastCalledWith("first");

    rerender(
      <ToolCardTerminalOutput outputText="second" isExpanded={true} isReady={true} testId="tool-terminal" />,
    );

    expect(terminal.write).toHaveBeenCalledTimes(2);
    expect(terminal.write).toHaveBeenLastCalledWith("second");
  });

  it("disposes terminal resources on unmount", () => {
    const { unmount } = render(
      <ToolCardTerminalOutput outputText="cleanup" isExpanded={true} isReady={true} testId="tool-terminal" />,
    );

    const terminal = mocked.terminalInstances[0];
    expect(terminal).toBeDefined();
    unmount();
    expect(terminal.dispose).toHaveBeenCalledTimes(1);
  });
});
