// @vitest-environment jsdom
/**
 * Verifies lifecycle-sensitive presentation for compact subagent run cards.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentRunCard } from "../AgentRunCard";
import type { AgentRunRecord } from "../../state/agent-stream-store";

function runWithStatus(
  status: AgentRunRecord["status"],
  overrides: Partial<AgentRunRecord> = {},
): AgentRunRecord {
  return {
    taskId: 7,
    agentRunId: "run-1",
    agentId: "pathfinder",
    agentKind: "scout",
    agentDisplayName: "Pathfinder",
    agentIconKey: "pathfinder",
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
    ...overrides,
  };
}

describe("AgentRunCard", () => {
  it("animates only while the subagent is working", () => {
    const { container, rerender } = render(
      <AgentRunCard run={runWithStatus("running")} onOpen={vi.fn()} />,
    );

    const card = screen.getByTestId("agent-run-card-run-1");
    expect(card.querySelector("svg.animate-spin")).not.toBeNull();

    rerender(
      <AgentRunCard run={runWithStatus("completed")} onOpen={vi.fn()} />,
    );

    expect(card.querySelector("svg.animate-spin")).toBeNull();
    expect(
      container.querySelector('img[data-agent-icon-key="pathfinder"]'),
    ).not.toBeNull();
  });

  it("renders a synthetic second agent through the generic identity resolver", () => {
    const { container } = render(
      <AgentRunCard
        run={runWithStatus("completed", {
          agentRunId: "review-run-1",
          agentId: "reviewer",
          agentKind: "review",
          agentDisplayName: "Reviewer",
          agentIconKey: "reviewer",
        })}
        onOpen={vi.fn()}
      />,
    );

    expect(screen.getByText("Reviewer")).toBeTruthy();
    expect(
      container.querySelector(
        '[data-agent-id="reviewer"][data-agent-icon-key="reviewer"]',
      ),
    ).not.toBeNull();
  });
});
