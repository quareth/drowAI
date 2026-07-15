// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TurnActivityCard } from "@/components/chat/TurnActivityCard";
import type { TurnActivityBlock } from "@/components/chat/turnActivityBlocks";
import type { ChatMessage } from "@/components/chat/types";
import type { MessageGroup } from "@/hooks/useMessageGrouping";

const mocked = vi.hoisted(() => ({
  rendererMock: vi.fn(),
}));

vi.mock("@/components/chat/MessageGroup", () => ({
  MessageGroupRenderer: (props: { group: MessageGroup }) => {
    mocked.rendererMock(props);
    return <div data-testid={`expanded-${props.group.primaryType}`}>{props.group.key}</div>;
  },
}));

afterEach(() => {
  cleanup();
  mocked.rendererMock.mockReset();
});

function makeMessage(
  id: string,
  stepType: string,
  metadata: Record<string, unknown> = {},
): ChatMessage {
  return {
    id,
    type: "agent",
    content: "",
    timestamp: "2024-01-01T00:00:00Z",
    metadata: {
      step_type: stepType,
      turn_sequence: 1,
      id: "turn-1",
      ...metadata,
    },
  };
}

function makeGroup(key: string, primaryType: MessageGroup["primaryType"]): MessageGroup {
  const stepType =
    primaryType === "tool"
      ? "tool_start"
      : primaryType === "observation"
        ? "observation_delta"
        : "reasoning_delta";
  return {
    key,
    ind: primaryType === "reasoning" ? 0 : 1,
    primaryType,
    messages: [makeMessage(`${key}-message`, stepType)],
  };
}

function makeBlock(turnKey: string): TurnActivityBlock {
  return {
    type: "activity",
    key: `activity-${turnKey}`,
    turnKey,
    groups: [
      makeGroup(`${turnKey}-reasoning`, "reasoning"),
      makeGroup(`${turnKey}-tool`, "tool"),
      makeGroup(`${turnKey}-observation`, "observation"),
    ],
    summary: {
      toolCount: 3,
      thoughtCount: 1,
      observationCount: 1,
    },
  };
}

describe("TurnActivityCard", () => {
  it("renders collapsed summary by default", () => {
    render(<TurnActivityCard block={makeBlock("summary")} taskId={42} />);

    expect(screen.getByText("3 tools, 1 thought, 1 observation")).toBeTruthy();
    expect(screen.queryByTestId("turn-activity-details-summary")).toBeNull();
    expect(mocked.rendererMock).not.toHaveBeenCalled();
  });

  it("expands to render existing message group details", () => {
    render(<TurnActivityCard block={makeBlock("expand")} taskId={42} />);

    fireEvent.click(screen.getByRole("button", { name: /3 tools/ }));

    expect(screen.getByTestId("turn-activity-details-expand")).toBeTruthy();
    expect(screen.getByTestId("expanded-reasoning")).toBeTruthy();
    expect(screen.getByTestId("expanded-tool")).toBeTruthy();
    expect(screen.getByTestId("expanded-observation")).toBeTruthy();
    expect(mocked.rendererMock).toHaveBeenCalledTimes(3);
  });

  it("splits multi-call batch groups into individual transcript tool groups on expand", () => {
    const block = makeBlock("split");
    block.groups = [
      {
        key: "split-batch",
        ind: 1,
        primaryType: "tool",
        messages: [
          makeMessage("start-a", "tool_start", {
            tool_batch_id: "tb-1",
            tool_call_id: "tc-a",
          }),
          makeMessage("end-a", "tool_end", {
            tool_batch_id: "tb-1",
            tool_call_id: "tc-a",
          }),
          makeMessage("start-b", "tool_start", {
            tool_batch_id: "tb-1",
            tool_call_id: "tc-b",
          }),
          makeMessage("end-b", "tool_end", {
            tool_batch_id: "tb-1",
            tool_call_id: "tc-b",
          }),
        ],
      },
    ];

    render(<TurnActivityCard block={block} taskId={42} />);
    fireEvent.click(screen.getByRole("button", { name: /3 tools/ }));

    const renderedGroups = mocked.rendererMock.mock.calls.map((call) => call[0].group);
    expect(renderedGroups).toHaveLength(2);
    expect(renderedGroups.map((group) => group.key)).toEqual([
      "split-batch-tool-tc-a",
      "split-batch-tool-tc-b",
    ]);
    expect(renderedGroups.map((group) => group.messages.map((message: ChatMessage) => message.id))).toEqual([
      ["start-a", "end-a"],
      ["start-b", "end-b"],
    ]);
  });

  it("keeps expansion state for the same turn key across rerender", () => {
    const { rerender } = render(<TurnActivityCard block={makeBlock("stable")} taskId={42} />);

    fireEvent.click(screen.getByRole("button", { name: /3 tools/ }));
    expect(screen.getByTestId("turn-activity-details-stable")).toBeTruthy();

    rerender(<TurnActivityCard block={makeBlock("stable")} taskId={42} />);

    expect(screen.getByTestId("turn-activity-details-stable")).toBeTruthy();
  });
});
