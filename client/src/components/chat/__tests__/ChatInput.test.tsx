/**
 * ChatInput send/stop control tests.
 *
 * Responsibility: pin the composer button mode switch without involving the
 * full UnifiedAgentChat surface or stream transport.
 */
// @vitest-environment jsdom
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ChatInput from "@/components/chat/ChatInput";
import type { ChatMode } from "@/components/chat/types";

const mode: ChatMode = {
  canSendMessages: true,
  inputPlaceholder: "Type a message",
  inputDisabled: false,
};

afterEach(() => {
  cleanup();
});

describe("ChatInput", () => {
  it("renders send control when idle", () => {
    const onSend = vi.fn();

    render(
      <ChatInput
        value="hello"
        onChange={() => undefined}
        onSend={onSend}
        mode={mode}
      />,
    );

    const send = screen.getByRole("button", { name: "Send message" });
    fireEvent.click(send);

    expect(onSend).toHaveBeenCalledWith("hello");
    expect(screen.queryByRole("button", { name: "Stop generation" })).toBeNull();
  });

  it("renders stop control while running with an empty draft", () => {
    const onSend = vi.fn();
    const onStop = vi.fn();

    render(
      <ChatInput
        value=""
        onChange={() => undefined}
        onSend={onSend}
        onStop={onStop}
        mode={mode}
        isRunning
      />,
    );

    const stop = screen.getByRole("button", { name: "Stop generation" });
    fireEvent.click(stop);

    expect(onStop).toHaveBeenCalledOnce();
    expect(onSend).not.toHaveBeenCalled();
    expect(stop.className).not.toContain("rose");
  });

  it("renders send control while running with a draft", () => {
    const onSend = vi.fn();
    const onStop = vi.fn();

    render(
      <ChatInput
        value="new prompt"
        onChange={() => undefined}
        onSend={onSend}
        onStop={onStop}
        mode={mode}
        isRunning
      />,
    );

    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    const send = screen.getByRole("button", { name: "Send message" });
    fireEvent.click(send);

    expect(input.disabled).toBe(false);
    expect(onSend).toHaveBeenCalledWith("new prompt");
    expect(onStop).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Stop generation" })).toBeNull();
  });

  it("disables stop control while stopping", () => {
    const onStop = vi.fn();

    render(
      <ChatInput
        value=""
        onChange={() => undefined}
        onSend={() => undefined}
        onStop={onStop}
        mode={mode}
        isRunning
        isStopping
      />,
    );

    const stop = screen.getByRole("button", { name: "Stop generation" }) as HTMLButtonElement;
    expect(stop.disabled).toBe(true);
    fireEvent.click(stop);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("gates submission without clearing the current draft", () => {
    const onSend = vi.fn();

    render(
      <ChatInput
        value="draft survives compaction"
        onChange={() => undefined}
        onSend={onSend}
        mode={mode}
        submissionDisabled
        statusMessage="Compacting conversation context…"
      />,
    );

    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    const send = screen.getByRole("button", { name: "Send message" });

    expect(input.value).toBe("draft survives compaction");
    expect(input.disabled).toBe(false);
    expect((send as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toBe(
      "Compacting conversation context…",
    );
    fireEvent.click(send);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("preserves the controlled draft while the plan mode rerenders", () => {
    function ModeDraftHarness() {
      const [value, setValue] = useState("");
      const [planMode, setPlanMode] = useState(false);

      return (
        <ChatInput
          value={value}
          onChange={setValue}
          onSend={() => undefined}
          mode={mode}
          primaryMode="agent_full"
          planMode={planMode}
          onPrimaryModeChange={() => undefined}
          onPlanModeChange={setPlanMode}
        />
      );
    }

    render(<ModeDraftHarness />);

    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(input, {
      target: { value: "draft survives mode transition" },
    });
    fireEvent.click(screen.getByTestId("chat-plan-toggle"));

    expect(
      screen.getByTestId("chat-plan-toggle").getAttribute("aria-pressed"),
    ).toBe("true");
    expect(input.value).toBe("draft survives mode transition");
  });
});
