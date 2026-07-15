import { afterEach, describe, expect, it } from "vitest";

import { filterChatMessages } from "@/utils/chatFilters";
import { featureFlags } from "@/config/feature-flags";
import type { ChatMessage } from "@/components/chat/types";

const baseMessage = (overrides: Partial<ChatMessage> = {}): ChatMessage => ({
  id: `msg-${Math.random().toString(36).slice(2)}`,
  type: "agent",
  content: "x",
  timestamp: "2026-01-01T00:00:00.000Z",
  isStreaming: false,
  metadata: {},
  ...overrides,
});

const originalBasicChat = featureFlags.enableBasicChat;

afterEach(() => {
  featureFlags.enableBasicChat = originalBasicChat;
});

describe("filterChatMessages observation parity", () => {
  it("keeps observation events without conversation metadata", () => {
    featureFlags.enableBasicChat = true;
    const input = [
      baseMessage({
        metadata: {
          step_type: "observation_delta",
        },
      }),
    ];

    const filtered = filterChatMessages(input);
    expect(filtered).toHaveLength(1);
  });

  it("keeps reasoning/tool events and still filters regular orphan messages", () => {
    featureFlags.enableBasicChat = true;
    const input = [
      baseMessage({
        id: "reasoning",
        metadata: { step_type: "reasoning_delta" },
      }),
      baseMessage({
        id: "tool",
        metadata: { step_type: "tool_start" },
      }),
      baseMessage({
        id: "orphan",
        metadata: { step_type: "message_delta" },
      }),
    ];

    const filtered = filterChatMessages(input);
    expect(filtered.map((item) => item.id)).toEqual(["reasoning", "tool"]);
  });
});
