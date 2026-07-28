// @vitest-environment jsdom
/**
 * Verifies lifecycle-sensitive presentation for compact subagent run cards.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentRunCard } from "../AgentRunCard";
import type { AgentRunRecord } from "../../state/agent-stream-store";

function runWithStatus(status: AgentRunRecord["status"]): AgentRunRecord {
  return {
    taskId: 7,
    agentRunId: "run-1",
    agentId: "pathfinder",
    agentKind: "scout",
    agentDisplayName: "Pathfinder",
    status,
    lifecycleVersion: 1,
    conversationId: "conversation-1",
    parentTurnId: "turn-1",
    parentRunId: null,
    assignment: {
      objective: "Scan localhost",
      scope_summary: "Check the PostgreSQL port",
    },
    result: null,
    safeError: null,
    firstSequence: 1,
    lastSequence: 1,
    createdAt: 1,
    completedAt: status === "completed" ? 2 : null,
    updatedAt: 2,
    activity: [],
  };
}

describe("AgentRunCard", () => {
  it("animates only while the subagent is working", () => {
    const { rerender } = render(
      <AgentRunCard run={runWithStatus("running")} onOpen={vi.fn()} />,
    );

    const card = screen.getByTestId("agent-run-card-run-1");
    expect(card.querySelector("svg.animate-spin")).not.toBeNull();

    rerender(
      <AgentRunCard run={runWithStatus("completed")} onOpen={vi.fn()} />,
    );

    expect(card.querySelector("svg.animate-spin")).toBeNull();
  });
});
