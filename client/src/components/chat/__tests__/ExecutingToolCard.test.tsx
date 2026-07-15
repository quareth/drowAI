// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ExecutingToolCard } from "@/components/chat/ExecutingToolCard";

const mocked = vi.hoisted(() => ({
  useToolRawOutputMock: vi.fn(),
}));

vi.mock("@/components/chat/tool-card-terminal/useToolRawOutput", () => ({
  useToolRawOutput: mocked.useToolRawOutputMock,
}));

vi.mock("@/components/chat/tool-card-terminal/ToolCardTerminalOutput", () => ({
  ToolCardTerminalOutput: ({ outputText, testId }: { outputText: string; testId?: string }) => (
    <div data-testid={testId ?? "tool-card-terminal-output"}>{outputText}</div>
  ),
}));

afterEach(() => {
  cleanup();
  mocked.useToolRawOutputMock.mockReset();
});

function getToggleButton(testId = "tool-card"): HTMLElement {
  return within(screen.getByTestId(testId)).getByRole("button", { name: "Toggle tool output" });
}

describe("ExecutingToolCard", () => {
  it("keeps tool card shell style aligned with chat card layout", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "loading" },
      status: "loading",
      isLoading: true,
      isReady: false,
      isNotAvailable: false,
      isError: false,
    });

    render(<ExecutingToolCard toolName="nmap" status="executing" testId="tool-card" />);

    const card = screen.getByTestId("tool-card");
    expect(card.className).toContain("inline-block");
    expect(card.className).toContain("max-w-[calc(100%-2rem)]");
    expect(card.className).toContain("rounded-lg");
    expect(card.className).toContain("bg-slate-950/40");
  });

  it("is non-expandable while tool is executing", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "loading" },
      status: "loading",
      isLoading: true,
      isReady: false,
      isNotAvailable: false,
      isError: false,
    });

    render(<ExecutingToolCard toolName="nmap" status="executing" testId="tool-card" />);

    const toggle = getToggleButton();
    expect(toggle.getAttribute("disabled")).not.toBeNull();
  });

  it("is expandable while completed card output is loading", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "loading" },
      status: "loading",
      isLoading: true,
      isReady: false,
      isNotAvailable: false,
      isError: false,
    });

    render(
      <ExecutingToolCard
        toolName="nmap"
        status="completed"
        taskId={1}
        toolCallId="call-1"
        testId="tool-card"
      />,
    );

    const toggle = getToggleButton();
    expect(toggle.getAttribute("disabled")).toBeNull();
  });

  it("keeps expanded state during first-load and transitions to ready output", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "loading" },
      status: "loading",
      isLoading: true,
      isReady: false,
      isNotAvailable: false,
      isError: false,
    });

    const view = render(
      <ExecutingToolCard
        toolName="nmap"
        status="completed"
        taskId={1}
        toolCallId="call-first-open"
        testId="tool-card"
      />,
    );

    const toggle = getToggleButton();
    fireEvent.click(toggle);
    expect(screen.getByText("Loading raw output...")).not.toBeNull();

    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "ready", outputText: "scan output ready" },
      status: "ready",
      isLoading: false,
      isReady: true,
      isNotAvailable: false,
      isError: false,
    });
    view.rerender(
      <ExecutingToolCard
        toolName="nmap"
        status="completed"
        taskId={1}
        toolCallId="call-first-open"
        testId="tool-card"
      />,
    );

    expect(screen.queryByText("Loading raw output...")).toBeNull();
    expect(screen.getByText("scan output ready")).not.toBeNull();
  });

  it("uses the full available width while expanded", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "loading" },
      status: "loading",
      isLoading: true,
      isReady: false,
      isNotAvailable: false,
      isError: false,
    });

    render(
      <ExecutingToolCard
        toolName="information_gathering.network_discovery.nmap"
        status="completed"
        taskId={1}
        toolCallId="call-expanded"
        testId="tool-card"
      />,
    );

    fireEvent.click(getToggleButton());

    const card = screen.getByTestId("tool-card");
    expect(card.className).toContain("block");
    expect(card.className).toContain("w-full");
    expect(card.className).toContain("max-w-[calc(100%-2rem)]");
    expect(screen.getByText("Completed").className).toContain("whitespace-nowrap");
  });

  it("becomes expandable and renders terminal when output is ready", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: {
        status: "ready",
        outputText: "scan output",
      },
      status: "ready",
      isLoading: false,
      isReady: true,
      isNotAvailable: false,
      isError: false,
    });

    render(
      <ExecutingToolCard
        toolName="nmap"
        status="completed"
        taskId={1}
        toolCallId="call-2"
        testId="tool-card"
      />,
    );

    const toggle = getToggleButton();
    expect(toggle.getAttribute("disabled")).toBeNull();
    fireEvent.click(toggle);
    expect(screen.getByTestId("tool-card-terminal")).not.toBeNull();
    expect(screen.getByText("scan output")).not.toBeNull();
  });

  it("uses monospace restrained-contrast fallback text for unavailable output", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "not_available", reason: "execution_not_found" },
      status: "not_available",
      isLoading: false,
      isReady: false,
      isNotAvailable: true,
      isError: false,
    });

    render(
      <ExecutingToolCard
        toolName="nmap"
        status="completed"
        taskId={1}
        toolCallId="call-3"
        testId="tool-card"
      />,
    );

    const toggle = getToggleButton();
    fireEvent.click(toggle);

    const fallback = screen.getByText("Raw output unavailable: execution record not found.");
    expect(fallback.className).toContain("font-mono");
    expect(fallback.className).toContain("text-slate-400/90");
  });

  it("shows explicit missing-artifact fallback reason", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "not_available", reason: "artifact_not_found" },
      status: "not_available",
      isLoading: false,
      isReady: false,
      isNotAvailable: true,
      isError: false,
    });

    render(
      <ExecutingToolCard
        toolName="nmap"
        status="completed"
        taskId={1}
        toolCallId="call-4"
        testId="tool-card"
      />,
    );

    const toggle = getToggleButton();
    fireEvent.click(toggle);

    expect(screen.getByText("Raw output unavailable: referenced artifact data is missing.")).not.toBeNull();
  });

  it("renders retrieval-error fallback when resolver returns error state", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "error", message: "network issue" },
      status: "error",
      isLoading: false,
      isReady: false,
      isNotAvailable: false,
      isError: true,
    });

    render(
      <ExecutingToolCard
        toolName="nmap"
        status="failed"
        taskId={1}
        toolCallId="call-5"
        testId="tool-card"
      />,
    );

    const toggle = getToggleButton();
    fireEvent.click(toggle);

    expect(screen.getByText("Raw output unavailable due to a retrieval error.")).not.toBeNull();
  });

  it("defers raw output loading until card is expanded", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "ready", outputText: "prefetched" },
      status: "ready",
      isLoading: false,
      isReady: true,
      isNotAvailable: false,
      isError: false,
    });

    render(
      <ExecutingToolCard
        toolName="nmap"
        status="completed"
        taskId={42}
        toolCallId="call-expand"
        testId="tool-card"
      />,
    );

    expect(mocked.useToolRawOutputMock).toHaveBeenCalledWith(
      expect.objectContaining({
        taskId: 42,
        toolCallId: "call-expand",
        enabled: false,
      }),
    );

    const toggle = getToggleButton();
    fireEvent.click(toggle);

    expect(mocked.useToolRawOutputMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        taskId: 42,
        toolCallId: "call-expand",
        enabled: true,
      }),
    );
  });
});
