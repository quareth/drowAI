// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToolBatchCard } from "@/components/chat/ToolBatchCard";
import type { ChatMessage } from "@/components/chat/types";

const mocked = vi.hoisted(() => ({
  useToolRawOutputMock: vi.fn(() => ({
    state: { status: "loading" },
    status: "loading",
    isLoading: true,
    isReady: false,
    isNotAvailable: false,
    isError: false,
  })),
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
});

function makeMsg(
  overrides: Partial<ChatMessage> & { metadata: Record<string, unknown> },
): ChatMessage {
  return {
    type: "agent",
    content: "",
    timestamp: "2024-01-01T00:00:00Z",
    isStreaming: false,
    ...overrides,
  } as ChatMessage;
}

describe("ToolBatchCard", () => {
  it("renders rows in manifest order regardless of completion order", () => {
    const messages: ChatMessage[] = [
      // batch_start with manifest order: c, a, b
      makeMsg({
        id: "batch-start",
        metadata: {
          step_type: "tool_batch_start",
          tool_batch_id: "tb_1",
          tool_calls: [
            { tool_call_id: "tc_c", tool_id: "tool.c" },
            { tool_call_id: "tc_a", tool_id: "tool.a" },
            { tool_call_id: "tc_b", tool_id: "tool.b" },
          ],
        },
      }),
      // tool events arrive in different order (b first, then a, then c)
      makeMsg({
        id: "start-b",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "tc_b",
          tool_batch_id: "tb_1",
          tool_name: "tool.b",
        },
      }),
      makeMsg({
        id: "end-b",
        metadata: {
          step_type: "tool_end",
          tool_call_id: "tc_b",
          tool_batch_id: "tb_1",
          tool_name: "tool.b",
          status: "success",
        },
      }),
      makeMsg({
        id: "start-a",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "tc_a",
          tool_batch_id: "tb_1",
          tool_name: "tool.a",
        },
      }),
      makeMsg({
        id: "end-a",
        metadata: {
          step_type: "tool_end",
          tool_call_id: "tc_a",
          tool_batch_id: "tb_1",
          tool_name: "tool.a",
          status: "success",
        },
      }),
      makeMsg({
        id: "start-c",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "tc_c",
          tool_batch_id: "tb_1",
          tool_name: "tool.c",
        },
      }),
      makeMsg({
        id: "end-c",
        metadata: {
          step_type: "tool_end",
          tool_call_id: "tc_c",
          tool_batch_id: "tb_1",
          tool_name: "tool.c",
          status: "error",
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="grp1" taskId={42} />);

    const rows = screen.getAllByTestId(/tool-batch-card-grp1-row-/);
    // Manifest order: c, a, b
    expect(rows.map((row) => row.getAttribute("data-testid"))).toEqual([
      "tool-batch-card-grp1-row-tc_c",
      "tool-batch-card-grp1-row-tc_a",
      "tool-batch-card-grp1-row-tc_b",
    ]);
  });

  it("does not compound standalone tool-card width constraints inside a batch", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "batch-start",
        metadata: {
          step_type: "tool_batch_start",
          tool_batch_id: "tb_width",
          tool_calls: [
            { tool_call_id: "tc_80", tool_id: "information_gathering.network_discovery.nmap" },
            { tool_call_id: "tc_443", tool_id: "information_gathering.network_discovery.nmap" },
          ],
        },
      }),
      makeMsg({
        id: "batch-end",
        metadata: {
          step_type: "tool_batch_end",
          tool_batch_id: "tb_width",
          status: "completed",
          results: [
            { tool_call_id: "tc_80", tool: "information_gathering.network_discovery.nmap", status: "success" },
            { tool_call_id: "tc_443", tool: "information_gathering.network_discovery.nmap", status: "success" },
          ],
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="width" taskId={42} />);

    const batch = screen.getByTestId("tool-batch-card-width");
    expect(batch.className).toContain("w-full");
    expect(batch.className).toContain("max-w-[calc(100%-2rem)]");

    const firstRow = screen.getByTestId("tool-batch-card-width-row-tc_80");
    expect(firstRow.className).toContain("w-full");
    expect(firstRow.className).toContain("max-w-full");
    expect(firstRow.className).not.toContain("max-w-[calc(70%-2rem)]");

    const completedLabels = screen.getAllByText("Completed");
    expect(completedLabels.some((label) => label.className.includes("whitespace-nowrap"))).toBe(true);
  });

  it("falls back to first-seen order when manifest is absent", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "start-1",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "tc_first",
          tool_name: "tool.first",
        },
      }),
      makeMsg({
        id: "start-2",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "tc_second",
          tool_name: "tool.second",
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="grp2" />);
    const rows = screen.getAllByTestId(/tool-batch-card-grp2-row-/);
    expect(rows.map((row) => row.getAttribute("data-testid"))).toEqual([
      "tool-batch-card-grp2-row-tc_first",
      "tool-batch-card-grp2-row-tc_second",
    ]);
  });

  it("renders single-call batch identically to ExecutingToolCard (no header)", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "start",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "only",
          tool_name: "tool.only",
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="solo" />);

    // The wrapper only emits the batch header for multi-row batches.
    expect(screen.queryByText("Batch")).toBeNull();
    expect(screen.getByTestId("tool-batch-card-solo-row-only")).not.toBeNull();
  });

  it("surfaces aggregate status from tool_batch_end", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "batch-start",
        metadata: {
          step_type: "tool_batch_start",
          tool_batch_id: "tb_2",
          effective_execution_strategy: "sequential",
          tool_calls: [
            { tool_call_id: "tc_x", tool_id: "tool.x" },
            { tool_call_id: "tc_y", tool_id: "tool.y" },
          ],
        },
      }),
      makeMsg({
        id: "start-x",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "tc_x",
          tool_name: "tool.x",
        },
      }),
      makeMsg({
        id: "start-y",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "tc_y",
          tool_name: "tool.y",
        },
      }),
      makeMsg({
        id: "batch-end",
        metadata: {
          step_type: "tool_batch_end",
          tool_batch_id: "tb_2",
          status: "cancelled",
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="grp3" />);
    expect(screen.getByText(/Cancelled/)).toBeTruthy();
  });

  it("renders cancelled tool rows as stopped instead of running", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "start",
        metadata: {
          step_type: "tool_start",
          tool_call_id: "tc_stop",
          tool_name: "shell.exec",
        },
      }),
      makeMsg({
        id: "end",
        metadata: {
          step_type: "tool_end",
          tool_call_id: "tc_stop",
          tool_name: "shell.exec",
          status: "cancelled",
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="stopped" />);

    expect(screen.getByText("Stopped")).toBeTruthy();
    expect(screen.queryByText("Running")).toBeNull();
  });

  it("renders terminal batch rows when no per-tool events were emitted", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "batch-start",
        metadata: {
          step_type: "tool_batch_start",
          tool_batch_id: "tb_4",
          tool_calls: [
            { tool_call_id: "tc_rejected", tool_id: "tool.rejected" },
          ],
        },
      }),
      makeMsg({
        id: "batch-end",
        metadata: {
          step_type: "tool_batch_end",
          tool_batch_id: "tb_4",
          status: "failed",
          results: [
            { tool_call_id: "tc_rejected", tool: "tool.rejected", status: "failed" },
          ],
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="grp4" />);

    expect(screen.getByTestId("tool-batch-card-grp4-row-tc_rejected")).not.toBeNull();
  });
});
