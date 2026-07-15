// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ToolApprovalCard from "@/components/chat/ToolApprovalCard";

afterEach(() => {
  cleanup();
});

describe("ToolApprovalCard", () => {
  it("submits multi-call approval once with per-call decisions", () => {
    const onBatchSubmit = vi.fn();

    render(
      <ToolApprovalCard
        payload={{
          type: "tool_approval",
          tool_id: "tool.a",
          tool_name: "tool.a",
          parameters: {},
          description: "Run tools",
          tool_batch_id: "tb_1",
          items: [
            {
              tool_call_id: "tc_1",
              tool_id: "tool.a",
              tool_name: "tool.a",
              parameters: {},
            },
            {
              tool_call_id: "tc_2",
              tool_id: "tool.b",
              tool_name: "tool.b",
              parameters: {},
            },
          ],
        }}
        onApprove={vi.fn()}
        onEdit={vi.fn()}
        onSkip={vi.fn()}
        onBatchSubmit={onBatchSubmit}
      />,
    );

    const skipButtons = screen.getAllByRole("button", { name: /skip/i });
    fireEvent.click(skipButtons[1]);
    expect(onBatchSubmit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /run selected/i }));

    expect(onBatchSubmit).toHaveBeenCalledTimes(1);
    expect(onBatchSubmit).toHaveBeenCalledWith({
      action: "approve",
      tool_batch_id: "tb_1",
      decisions: [
        { tool_call_id: "tc_1", action: "approve", edited_parameters: undefined },
        { tool_call_id: "tc_2", action: "skip", edited_parameters: undefined },
      ],
    });
  });
});
