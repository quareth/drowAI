// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToolBatchCard } from "@/components/chat/ToolBatchCard";
import type { ChatMessage } from "@/components/chat/types";
import {
  normalizeTranscriptItemsToSteps,
  type ChatTranscriptItem,
} from "@/hooks/chat-history-bootstrap";

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

  it("keeps mixed chat-stop batch aggregate equal for live and replayed rows", () => {
    const liveMessages: ChatMessage[] = [
      makeMsg({
        id: "live-start",
        metadata: {
          step_type: "tool_batch_start",
          tool_batch_id: "tb_mixed_stop",
          tool_calls: [
            { tool_call_id: "tc_done", tool_id: "shell.exec" },
            { tool_call_id: "tc_stop", tool_id: "shell.exec" },
          ],
        },
      }),
      makeMsg({
        id: "live-done",
        metadata: {
          step_type: "tool_end",
          tool_batch_id: "tb_mixed_stop",
          tool_call_id: "tc_done",
          tool_name: "shell.exec",
          status: "completed",
        },
      }),
      makeMsg({
        id: "live-stop",
        metadata: {
          step_type: "tool_end",
          tool_batch_id: "tb_mixed_stop",
          tool_call_id: "tc_stop",
          tool_name: "shell.exec",
          status: "cancelled",
        },
      }),
      makeMsg({
        id: "live-batch-end",
        metadata: {
          step_type: "tool_batch_end",
          tool_batch_id: "tb_mixed_stop",
          status: "completed_with_errors",
          results: [
            { tool_call_id: "tc_done", tool: "shell.exec", status: "completed" },
            { tool_call_id: "tc_stop", tool: "shell.exec", status: "cancelled" },
          ],
        },
      }),
    ];

    const replayItems: ChatTranscriptItem[] = [
      {
        id: "replay-done",
        kind: "tool",
        turn_number: 3,
        content: "{}",
        metadata: {
          tool_batch_id: "tb_mixed_stop",
          tool_call_id: "tc_done",
          tool_name: "shell.exec",
          status: "completed",
        },
      },
      {
        id: "replay-stop",
        kind: "tool",
        turn_number: 3,
        content: "Tool stopped",
        metadata: {
          tool_batch_id: "tb_mixed_stop",
          tool_call_id: "tc_stop",
          tool_name: "shell.exec",
          status: "cancelled",
        },
      },
    ];
    const replayMessages = normalizeTranscriptItemsToSteps(42, replayItems).map((step, index) =>
      makeMsg({
        id: `replay-${index}`,
        content: typeof step.content === "string" ? step.content : "",
        metadata: step.metadata as Record<string, unknown>,
      }),
    );

    const live = render(<ToolBatchCard messages={liveMessages} groupKey="live-mixed-stop" />);
    expect(screen.getByText("Completed with errors")).toBeTruthy();
    live.unmount();

    render(<ToolBatchCard messages={replayMessages} groupKey="replay-mixed-stop" />);
    expect(screen.getByText("Completed with errors")).toBeTruthy();
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

  it("presents shell process states independently from generic tool success", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "batch-start",
        metadata: {
          step_type: "tool_batch_start",
          tool_batch_id: "tb_shell_states",
          tool_calls: [
            { tool_call_id: "tc_running", tool_id: "shell.exec" },
            { tool_call_id: "tc_completed", tool_id: "shell.write_stdin" },
            { tool_call_id: "tc_timeout", tool_id: "shell.exec" },
            { tool_call_id: "tc_terminated", tool_id: "shell.write_stdin" },
          ],
        },
      }),
      ...[
        ["tc_running", "success", "running"],
        ["tc_completed", "success", "completed"],
        ["tc_timeout", "error", "timed_out"],
        ["tc_terminated", "success", "terminated"],
      ].map(([toolCallId, status, processStatus]) =>
        makeMsg({
          id: `end-${toolCallId}`,
          metadata: {
            step_type: "tool_end",
            tool_call_id: toolCallId,
            tool_name: "shell.exec",
            status,
            process_status: processStatus,
          },
        }),
      ),
      makeMsg({
        id: "batch-end",
        metadata: {
          step_type: "tool_batch_end",
          tool_batch_id: "tb_shell_states",
          status: "completed_with_errors",
          results: [
            { tool_call_id: "tc_running", tool: "shell.exec", status: "success" },
            { tool_call_id: "tc_completed", tool: "shell.write_stdin", status: "success" },
            { tool_call_id: "tc_timeout", tool: "shell.exec", status: "failed" },
            { tool_call_id: "tc_terminated", tool: "shell.write_stdin", status: "success" },
          ],
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="shell-states" />);

    expect(screen.getByText("Session active")).toBeTruthy();
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
    expect(screen.getByText("Process timed out")).toBeTruthy();
    expect(screen.getByText("Process terminated")).toBeTruthy();
  });

  it("uses process status from batch-end-only shell rows", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "batch-end",
        metadata: {
          step_type: "tool_batch_end",
          tool_batch_id: "tb_shell_only",
          status: "completed",
          results: [
            {
              tool_call_id: "tc_running",
              tool: "shell.exec",
              status: "success",
              process_status: "running",
            },
          ],
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="shell-batch-only" />);

    expect(screen.getByText("Completed")).toBeTruthy();
    expect(screen.getByText("Session active")).toBeTruthy();
  });

  it("treats running shell lifecycle delta process state as authoritative", () => {
    const messages: ChatMessage[] = [
      makeMsg({
        id: "shell-start",
        metadata: {
          step_type: "tool_start",
          tool_batch_id: "tb_shell_progress",
          tool_call_id: "tc_shell_progress",
          tool_name: "shell.utility",
        },
      }),
      makeMsg({
        id: "shell-progress",
        content: "progress line",
        metadata: {
          step_type: "tool_delta",
          tool_batch_id: "tb_shell_progress",
          tool_call_id: "tc_shell_progress",
          tool_name: "shell.utility",
          status: "success",
          process_status: "running",
          session_status: "active",
          interaction_boundary: "output_available",
          output_persistence: "transient",
          shell_lifecycle_event: true,
          compact_tool_result: {
            schema_version: "2.0",
            tool: "shell.utility",
            status: "success",
            success: true,
            summary: "progress line",
            key_findings: [],
            errors: [],
            report_recommendations: [],
            structured_signals: [],
            decision_evidence: [],
            lossiness_risk: "high",
          },
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="shell-progress" taskId={42} />);

    expect(screen.getByText("Running")).toBeTruthy();
    expect(screen.getByText("Session active")).toBeTruthy();
    expect(screen.getByText("Agent reviewing output")).toBeTruthy();
    expect(screen.getByTestId("tool-batch-card-shell-progress-row-tc_shell_progress-terminal").textContent).toBe(
      "progress line",
    );
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

  it("renders transient compact output from the tool-end event", () => {
    mocked.useToolRawOutputMock.mockReturnValue({
      state: { status: "not_available", reason: "missing_output_artifacts" },
      status: "not_available",
      isLoading: false,
      isReady: false,
      isNotAvailable: true,
      isError: false,
    });
    const messages = [
      makeMsg({
        id: "utility-start",
        metadata: {
          step_type: "tool_start",
          tool_batch_id: "tb-utility",
          tool_call_id: "tc-utility",
          tool_name: "shell.utility",
          command_display: "touch /workspace/boris.txt",
        },
      }),
      makeMsg({
        id: "utility-end",
        metadata: {
          step_type: "tool_end",
          tool_batch_id: "tb-utility",
          tool_call_id: "tc-utility",
          tool_name: "shell.utility",
          status: "success",
          output_persistence: "transient",
          compact_tool_result: {
            schema_version: "2.0",
            tool: "shell.utility",
            status: "success",
            success: true,
            summary: "Created /workspace/boris.txt.",
            key_findings: [],
            errors: [],
            report_recommendations: [],
            structured_signals: [],
            decision_evidence: [],
            lossiness_risk: "low",
          },
        },
      }),
    ];

    render(<ToolBatchCard messages={messages} groupKey="utility" taskId={40} />);
    fireEvent.click(screen.getByRole("button", { name: "Toggle tool output" }));

    expect(screen.getByTestId("tool-batch-card-utility-row-tc-utility-terminal").textContent).toBe(
      "$ touch /workspace/boris.txt\nCreated /workspace/boris.txt.",
    );
    expect(screen.queryByText(/Raw output unavailable/)).toBeNull();
  });
});
