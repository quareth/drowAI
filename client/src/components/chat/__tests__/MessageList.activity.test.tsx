// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MessageList } from "@/components/chat/MessageList";
import type { ChatMessage } from "@/components/chat/types";
import type { MessageGroup } from "@/hooks/useMessageGrouping";

vi.mock("@/components/chat/MessageGroup", () => ({
  MessageGroupRenderer: ({ group }: { group: MessageGroup }) => (
    <div data-testid={`message-group-${group.primaryType}`}>
      {group.messages.map((message) => message.content).join("") || group.primaryType}
    </div>
  ),
}));

beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 0;
  });
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makeMessage(
  id: string,
  stepType: string,
  content: string,
  metadata: Record<string, unknown>,
): ChatMessage {
  return {
    id,
    type: "agent",
    content,
    timestamp: "2024-01-01T00:00:00Z",
    isStreaming: false,
    metadata: {
      step_type: stepType,
      turn_sequence: 1,
      id: "turn-1",
      ...metadata,
    },
  };
}

describe("MessageList activity collapse", () => {
  it("collapses completed-turn activity while keeping the final answer visible", () => {
    render(
      <MessageList
        messages={[
          makeMessage("reasoning", "reasoning_delta", "thinking", {
            ind: 0,
            reasoning_section_id: "turn-1:reasoning:0",
          }),
          makeMessage("reasoning-end", "reasoning_section_end", "", {
            ind: 0,
            reasoning_section_id: "turn-1:reasoning:0",
          }),
          makeMessage("tool-start", "tool_start", "", {
            ind: 1,
            tool_call_id: "tc-1",
          }),
          makeMessage("tool-end", "tool_end", "", {
            ind: 1,
            tool_call_id: "tc-1",
            status: "success",
          }),
          makeMessage("final", "message_delta", "Final answer", {
            ind: 2,
            final_snapshot: true,
          }),
        ]}
        taskId={42}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.getByTestId("turn-activity-card-turn-sequence:1")).toBeTruthy();
    expect(screen.getByText("Final answer")).toBeTruthy();
    expect(screen.queryByTestId("message-group-reasoning")).toBeNull();
    expect(screen.queryByTestId("message-group-tool")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /1 tool/ }));

    expect(screen.getByTestId("message-group-reasoning")).toBeTruthy();
    expect(screen.getByTestId("message-group-tool")).toBeTruthy();
  });
});
