// @vitest-environment jsdom
/**
 * Tests for retry rendering on direct chat message bubbles.
 *
 * These assertions keep retry UX scoped to retryable assistant error messages
 * instead of every error-status message.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MessageBubble } from "@/components/chat/MessageBubble";
import type { ChatMessage } from "@/components/chat/types";

const mocked = vi.hoisted(() => ({
  streamingContentMock: vi.fn(),
}));

vi.mock("@/components/ui/streaming-content", () => ({
  StreamingContent: (props: Record<string, unknown>) => {
    mocked.streamingContentMock(props);
    return <div data-testid="streaming-content" />;
  },
}));

// MessageBubble pulls user timezone via useAuth → AuthProvider; tests
// that render the bubble in isolation must stub the timezone hook so
// failure modes describe rendering behavior, not the missing provider.
vi.mock("@/hooks/use-user-timezone", () => ({
  useUserTimezone: () => "UTC",
}));

afterEach(() => {
  cleanup();
  mocked.streamingContentMock.mockReset();
});

function buildMessage(metadata: Record<string, unknown>): ChatMessage {
  return {
    id: "msg-1",
    type: "agent",
    content: "Something failed",
    timestamp: new Date().toISOString(),
    metadata,
  };
}

describe("MessageBubble", () => {
  it("renders provider refusals as an amber declined notice without retry", () => {
    const onRetry = vi.fn();
    render(
      <MessageBubble
        message={buildMessage({
          status: "declined",
          retryable: false,
          outcome_type: "provider_refusal",
          refusal: {
            provider: "anthropic",
            model: "claude-fable-5",
            category: "cyber",
            summary: "The provider declined this request under its cyber safety policy.",
            explanation: "Literal <b>provider</b> [link](https://example.com)",
            response_id: "msg_123",
            partial: false,
          },
        })}
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText("Declined")).toBeTruthy();
    expect(screen.getByText("claude-fable-5 declined this request")).toBeTruthy();
    expect(
      screen.getByText("The provider declined this request under its cyber safety policy."),
    ).toBeTruthy();
    fireEvent.click(screen.getByText("Provider details"));
    expect(
      screen.getByText("Literal <b>provider</b> [link](https://example.com)"),
    ).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry action" })).toBeNull();
    expect(onRetry).not.toHaveBeenCalled();
  });

  it("renders partial refusal content as explicitly incomplete", () => {
    const message = buildMessage({
      status: "declined",
      refusal: {
        provider: "openai",
        model: "gpt-5.6",
        summary: "The provider declined this request under its safety policy.",
        partial: true,
      },
    });
    message.content = "Partial answer";

    render(<MessageBubble message={message} />);

    expect(screen.getByText("Incomplete response")).toBeTruthy();
    expect(mocked.streamingContentMock).toHaveBeenCalledWith(
      expect.objectContaining({ content: "Partial answer", isStreaming: false }),
    );
  });

  it("bypasses streaming content renderer for error-status agent messages", () => {
    render(
      <MessageBubble
        message={buildMessage({ status: "error" })}
      />,
    );

    expect(screen.queryByTestId("streaming-content")).toBeNull();
    expect(mocked.streamingContentMock).not.toHaveBeenCalled();
  });

  it("renders retry only for retryable assistant errors", () => {
    const onRetry = vi.fn();
    const { rerender } = render(
      <MessageBubble
        message={buildMessage({ status: "error", retryable: false })}
        onRetry={onRetry}
      />,
    );

    expect(screen.queryByRole("button", { name: "Retry action" })).toBeNull();

    rerender(
      <MessageBubble
        message={buildMessage({ status: "error", retryable: true })}
        onRetry={onRetry}
      />,
    );

    const retryButton = screen.getByRole("button", { name: "Retry action" });
    fireEvent.click(retryButton);
    expect(onRetry).toHaveBeenCalledWith("msg-1");
  });

  // Phase 0 / Task 0.4 — pin the frontend retry state gap.
  //
  // The Phase 5.3 contract is that the retry button must stay disabled
  // from the moment the retry POST is accepted (or returns
  // ``already_in_flight``) until a terminal retry lifecycle event
  // arrives. Concretely:
  //
  //   * passing a ``retryState`` describing an active retry
  //     (``state="started" | "retrying" | "waiting_for_human"``) must
  //     disable the retry button so duplicate clicks issue no extra
  //     POST,
  //   * once the lifecycle terminates with ``state="failed"`` and the
  //     server still marks the message ``retryable``, the button
  //     re-enables,
  //   * a terminal ``completed``/``cancelled`` retry keeps the button
  //     disabled (no further retry CTA).
  //
  // Today ``MessageBubble`` knows nothing about retry lifecycle state;
  // it computes ``showRetry`` purely from ``metadata.status === "error"
  // && metadata.retryable``. So this test fails today because (a) the
  // ``retryState`` prop is not supported and (b) the rendered button
  // has no ``disabled`` attribute regardless of the lifecycle.
  it(
    "keeps retry button disabled while a retry lifecycle is in flight and re-enables on terminal failed",
    () => {
      const onRetry = vi.fn();
      const baseMessage = buildMessage({
        status: "error",
        retryable: true,
        turn_id: "task-1-turn-3",
      });

      // Active retry: button must render as disabled and a click must
      // NOT call onRetry.
      const { rerender } = render(
        <MessageBubble
          message={baseMessage}
          onRetry={onRetry}
          retryState={{
            taskId: 1,
            turnId: "task-1-turn-3",
            workflowId: 14,
            state: "started",
            retryAttempt: 1,
            retryMaxAttempts: 2,
            inFlight: true,
          }}
        />,
      );

      let retryButton = screen.getByRole("button", { name: "Retry action" });
      expect((retryButton as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(retryButton);
      expect(onRetry).not.toHaveBeenCalled();

      // Terminal failed but still retryable: button must re-enable so
      // the user can re-attempt within the retry budget.
      rerender(
        <MessageBubble
          message={baseMessage}
          onRetry={onRetry}
          retryState={{
            taskId: 1,
            turnId: "task-1-turn-3",
            workflowId: 14,
            state: "failed",
            retryAttempt: 1,
            retryMaxAttempts: 2,
            inFlight: false,
          }}
        />,
      );

      retryButton = screen.getByRole("button", { name: "Retry action" });
      expect((retryButton as HTMLButtonElement).disabled).toBe(false);
      fireEvent.click(retryButton);
      expect(onRetry).toHaveBeenCalledWith("msg-1");

      onRetry.mockReset();

      // Terminal completed: button stays disabled (no further CTA).
      rerender(
        <MessageBubble
          message={baseMessage}
          onRetry={onRetry}
          retryState={{
            taskId: 1,
            turnId: "task-1-turn-3",
            workflowId: 14,
            state: "completed",
            retryAttempt: 1,
            retryMaxAttempts: 2,
            inFlight: false,
          }}
        />,
      );

      retryButton = screen.getByRole("button", { name: "Retry action" });
      expect((retryButton as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(retryButton);
      expect(onRetry).not.toHaveBeenCalled();
    },
  );
});
