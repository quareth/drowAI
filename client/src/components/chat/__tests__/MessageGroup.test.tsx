// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MessageGroup } from "@/hooks/useMessageGrouping";
import { MessageGroupRenderer } from "@/components/chat/MessageGroup";

const mocked = vi.hoisted(() => ({
  executingToolCardMock: vi.fn(),
  thinkingCardMock: vi.fn(),
  observingCardMock: vi.fn(),
  messageBubbleMock: vi.fn(),
}));

vi.mock("@/components/chat/ExecutingToolCard", () => ({
  ExecutingToolCard: (props: Record<string, unknown>) => {
    mocked.executingToolCardMock(props);
    return <div data-testid="executing-tool-card" />;
  },
}));

vi.mock("@/components/chat/ThinkingCard", () => ({
  ThinkingCard: (props: Record<string, unknown>) => {
    mocked.thinkingCardMock(props);
    const testId = typeof props?.testId === "string" ? props.testId : "thinking-card";
    return <div data-testid={testId} />;
  },
}));

vi.mock("@/components/chat/ObservingCard", () => ({
  ObservingCard: (props: Record<string, unknown>) => {
    mocked.observingCardMock(props);
    const testId = typeof props?.testId === "string" ? props.testId : "observing-card";
    return <div data-testid={testId} />;
  },
}));

vi.mock("@/components/chat/MessageBubble", () => ({
  MessageBubble: (props: Record<string, unknown>) => {
    mocked.messageBubbleMock(props);
    return <div data-testid="message-bubble" />;
  },
}));

afterEach(() => {
  cleanup();
  mocked.executingToolCardMock.mockReset();
  mocked.thinkingCardMock.mockReset();
  mocked.observingCardMock.mockReset();
  mocked.messageBubbleMock.mockReset();
});

describe("MessageGroupRenderer", () => {
  it("passes parent taskId to tool cards when metadata.task_id is missing", () => {
    const group: MessageGroup = {
      key: "1:tool-call-a",
      ind: 1,
      primaryType: "tool",
      messages: [
        {
          id: "msg-tool-start",
          type: "agent",
          content: "Executing nmap...",
          timestamp: new Date().toISOString(),
          metadata: {
            step_type: "tool_start",
            tool_call_id: "call-a",
            tool: "nmap",
          },
        },
        {
          id: "msg-tool-end",
          type: "agent",
          content: "Tool nmap completed (success)",
          timestamp: new Date().toISOString(),
          metadata: {
            step_type: "tool_end",
            tool_call_id: "call-a",
            status: "success",
            tool: "nmap",
          },
        },
      ],
    };

    render(<MessageGroupRenderer group={group} taskId={77} />);
    expect(screen.getByTestId("executing-tool-card")).not.toBeNull();

    const props = mocked.executingToolCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.taskId).toBe(77);
    expect(props.toolCallId).toBe("call-a");
  });

  it("renders Thinking card shell on reasoning_start", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "1:r",
          ind: 1,
          primaryType: "reasoning",
          messages: [
            {
              id: "msg-reasoning-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "reasoning_start",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("reasoning-step-1:r")).toBeTruthy();
    const props = mocked.thinkingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.steps).toEqual([]);
  });

  it("supports camelCase metadata for tool_start/tool_end in tool groups", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "14:t",
          ind: 14,
          primaryType: "tool",
          messages: [
            {
              id: "msg-tool-start",
              type: "agent",
              content: "Executing nmap...",
              timestamp: new Date().toISOString(),
              metadata: {
                stepType: "tool_start",
                tool_call_id: "call-b",
                tool: "nmap",
              },
            },
            {
              id: "msg-tool-end",
              type: "agent",
              content: "Tool nmap completed (success)",
              timestamp: new Date().toISOString(),
              metadata: {
                stepType: "tool_end",
                tool_call_id: "call-b",
                status: "success",
                tool: "nmap",
              },
            },
          ],
        }}
      />,
    );

    const props = mocked.executingToolCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.status).toBe("completed");
    expect(props.toolCallId).toBe("call-b");
  });

  it("renders Thinking card on reasoning_start when producer uses camelCase stepType", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "11:r",
          ind: 11,
          primaryType: "reasoning",
          messages: [
            {
              id: "msg-reasoning-start-camel",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                stepType: "reasoning_start",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("reasoning-step-11:r")).toBeTruthy();
    const props = mocked.thinkingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.steps).toEqual([]);
  });

  it("renders Observing card shell on observation_start", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "2:o",
          ind: 2,
          primaryType: "observation",
          messages: [
            {
              id: "msg-observation-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_start",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("observation-card-2:o")).toBeTruthy();
    const props = mocked.observingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.hasContent).toBe(false);
  });

  it("renders Observing card on observation_start when producer uses camelCase stepType", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "13:o",
          ind: 13,
          primaryType: "observation",
          messages: [
            {
              id: "msg-observation-start-camel",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                stepType: "observation_start",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("observation-card-13:o")).toBeTruthy();
    const props = mocked.observingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.hasContent).toBe(false);
  });

  it("renders Thinking card shell with content after reasoning_delta", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "5:r",
          ind: 5,
          primaryType: "reasoning",
          messages: [
            {
              id: "msg-reasoning-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "reasoning_start",
              },
            },
            {
              id: "msg-reasoning-delta",
              type: "agent",
              content: "planning path",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "reasoning_delta",
              },
            },
          ],
        }}
      />,
    );

    const props = mocked.thinkingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.steps).toEqual(["planning path"]);
  });

  it("does not add a thinking step for an empty reasoning delta chunk", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "12:r",
          ind: 12,
          primaryType: "reasoning",
          messages: [
            {
              id: "msg-reasoning-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "reasoning_start",
              },
            },
            {
              id: "msg-reasoning-empty-delta",
              type: "agent",
              content: "   ",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "reasoning_delta",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("reasoning-step-12:r")).toBeTruthy();
    const props = mocked.thinkingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.steps).toEqual([]);
  });

  it("does not set observation hasContent for empty observation delta chunk", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "14:o",
          ind: 14,
          primaryType: "observation",
          messages: [
            {
              id: "msg-observation-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_start",
              },
            },
            {
              id: "msg-observation-empty-delta",
              type: "agent",
              content: "   ",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_delta",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("observation-card-14:o")).toBeTruthy();
    const props = mocked.observingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.hasContent).toBe(false);
  });

  it("renders Observation card shell with content after observation_delta", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "6:o",
          ind: 6,
          primaryType: "observation",
          messages: [
            {
              id: "msg-observation-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_start",
              },
            },
            {
              id: "msg-observation-delta",
              type: "agent",
              content: "result summary",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_delta",
              },
            },
          ],
        }}
      />,
    );

    const props = mocked.observingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.hasContent).toBe(true);
  });

  it("does not render reasoning card when only reasoning_start + reasoning_section_end exists", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "3:r",
          ind: 3,
          primaryType: "reasoning",
          messages: [
            {
              id: "msg-reasoning-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "reasoning_start",
              },
            },
            {
              id: "msg-reasoning-end",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "reasoning_section_end",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.queryByTestId("reasoning-step-3:r")).toBeNull();
    expect(mocked.thinkingCardMock).not.toHaveBeenCalled();
  });

  it("does not render observation card when only observation_start + observation_section_end exists", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "4:o",
          ind: 4,
          primaryType: "observation",
          messages: [
            {
              id: "msg-observation-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_start",
              },
            },
            {
              id: "msg-observation-end",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_section_end",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.queryByTestId("observation-card-4:o")).toBeNull();
    expect(mocked.observingCardMock).not.toHaveBeenCalled();
  });

  it("renders thinking card for start -> delta -> end lifecycle", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "7:r",
          ind: 7,
          primaryType: "reasoning",
          messages: [
            {
              id: "msg-reasoning-start",
              type: "agent",
              content: "",
              timestamp: "2026-04-16T10:00:00.000Z",
              metadata: {
                step_type: "reasoning_start",
              },
            },
            {
              id: "msg-reasoning-delta",
              type: "agent",
              content: "thinking content",
              timestamp: "2026-04-16T10:00:00.150Z",
              metadata: {
                step_type: "reasoning_delta",
              },
            },
            {
              id: "msg-reasoning-end",
              type: "agent",
              content: "",
              timestamp: "2026-04-16T10:00:00.400Z",
              metadata: {
                step_type: "reasoning_section_end",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("reasoning-step-7:r")).toBeTruthy();
    const props = mocked.thinkingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.steps).toEqual(["thinking content"]);
    expect(props.isInProgress).toBe(false);
    expect(props.durationMs).toBe(400);
  });

  it("keeps a coalesced reasoning burst active when its latest section is open", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "7:reasoning-burst",
          ind: 7,
          primaryType: "reasoning",
          messages: [
            {
              id: "reasoning-1-start",
              type: "agent",
              content: "",
              timestamp: "2026-04-16T10:00:00.000Z",
              metadata: { step_type: "reasoning_start" },
            },
            {
              id: "reasoning-1-delta",
              type: "agent",
              content: "Analyzing request.",
              timestamp: "2026-04-16T10:00:00.100Z",
              metadata: { step_type: "reasoning_delta" },
            },
            {
              id: "reasoning-1-end",
              type: "agent",
              content: "",
              timestamp: "2026-04-16T10:00:00.200Z",
              metadata: { step_type: "reasoning_section_end" },
            },
            {
              id: "reasoning-2-start",
              type: "agent",
              content: "",
              timestamp: "2026-04-16T10:00:00.300Z",
              metadata: { step_type: "reasoning_start" },
            },
            {
              id: "reasoning-2-delta",
              type: "agent",
              content: "Preparing execution.",
              timestamp: "2026-04-16T10:00:00.400Z",
              metadata: { step_type: "reasoning_delta" },
            },
          ],
        }}
      />,
    );

    const props = mocked.thinkingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.isInProgress).toBe(true);
    expect(props.steps).toEqual(["Analyzing request.", "Preparing execution."]);
  });

  it("does not surface a fake 0.0s duration when reasoning boundaries collapse", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "7:r-collapsed",
          ind: 7,
          primaryType: "reasoning",
          messages: [
            {
              id: "msg-reasoning-start",
              type: "agent",
              content: "",
              timestamp: "2026-04-16T10:00:00.000Z",
              metadata: {
                step_type: "reasoning_start",
              },
            },
            {
              id: "msg-reasoning-delta",
              type: "agent",
              content: "thinking content",
              timestamp: "2026-04-16T10:00:00.000Z",
              metadata: {
                step_type: "reasoning_delta",
              },
            },
            {
              id: "msg-reasoning-end",
              type: "agent",
              content: "",
              timestamp: "2026-04-16T10:00:00.000Z",
              metadata: {
                step_type: "reasoning_section_end",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("reasoning-step-7:r-collapsed")).toBeTruthy();
    const props = mocked.thinkingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.durationMs).toBeUndefined();
  });

  it("renders observation card for start -> delta -> end lifecycle", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "8:o",
          ind: 8,
          primaryType: "observation",
          messages: [
            {
              id: "msg-observation-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_start",
              },
            },
            {
              id: "msg-observation-delta",
              type: "agent",
              content: "observation content",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_delta",
              },
            },
            {
              id: "msg-observation-end",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_section_end",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("observation-card-8:o")).toBeTruthy();
    const props = mocked.observingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.hasContent).toBe(true);
    expect(props.isInProgress).toBe(false);
  });

  it("renders reasoning card for delta-only input replay", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "9:r",
          ind: 9,
          primaryType: "reasoning",
          messages: [
            {
              id: "msg-reasoning-delta",
              type: "agent",
              content: "replayed thinking text",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "reasoning_delta",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("reasoning-step-9:r")).toBeTruthy();
    const props = mocked.thinkingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.steps).toEqual(["replayed thinking text"]);
  });

  it("renders observation card for delta-only input replay", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "10:o",
          ind: 10,
          primaryType: "observation",
          messages: [
            {
              id: "msg-observation-delta",
              type: "agent",
              content: "replayed observation",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "observation_delta",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("observation-card-10:o")).toBeTruthy();
    const props = mocked.observingCardMock.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(props.hasContent).toBe(true);
  });

  it("prefers final error message content over accumulated message deltas", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "11:m",
          ind: 11,
          primaryType: "message",
          messages: [
            {
              id: "msg-delta",
              type: "agent",
              content: "[]",
              isStreaming: true,
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "message_delta",
              },
            },
            {
              id: "msg-final",
              type: "agent",
              content: "[Error] Failed to continue.",
              timestamp: new Date().toISOString(),
              metadata: {
                status: "error",
                step_type: "assistant_final",
              },
            },
          ],
        }}
      />,
    );

    const props = mocked.messageBubbleMock.mock.calls[0]?.[0] as Record<string, unknown>;
    const message = props.message as { content: string; isStreaming?: boolean };
    expect(message.content).toBe("[Error] Failed to continue.");
    expect(message.isStreaming).toBe(false);
  });

  it("prefers final declined content and closes the stream", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "11:declined",
          ind: 11,
          primaryType: "message",
          messages: [
            {
              id: "msg-delta",
              type: "agent",
              content: "Partial answer",
              isStreaming: true,
              timestamp: new Date().toISOString(),
              metadata: { step_type: "message_delta" },
            },
            {
              id: "msg-final",
              type: "agent",
              content: "Partial answer",
              timestamp: new Date().toISOString(),
              metadata: {
                status: "declined",
                stop_reason: "refusal",
                step_type: "assistant_final",
              },
            },
          ],
        }}
      />,
    );

    const props = mocked.messageBubbleMock.mock.calls[0]?.[0] as Record<string, unknown>;
    const message = props.message as { content: string; isStreaming?: boolean };
    expect(message.content).toBe("Partial answer");
    expect(message.isStreaming).toBe(false);
  });

  it("does not render an empty non-error message shell", () => {
    render(
      <MessageGroupRenderer
        group={{
          key: "12:m",
          ind: 12,
          primaryType: "message",
          messages: [
            {
              id: "msg-start",
              type: "agent",
              content: "",
              timestamp: new Date().toISOString(),
              metadata: {
                step_type: "message_start",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.queryByTestId("message-bubble")).toBeNull();
    expect(mocked.messageBubbleMock).not.toHaveBeenCalled();
  });
});
