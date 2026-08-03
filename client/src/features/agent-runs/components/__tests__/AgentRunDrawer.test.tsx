// @vitest-environment jsdom
/**
 * Verifies drawer presentation stays driven by generic subagent identity metadata.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentAssignment, AgentRunLifecycleProjection } from "../../contracts/agent-run";
import {
  applyAgentRunLifecycleUpdate,
  resetAgentRunStoreForTests,
} from "../../state/agent-stream-store";
import {
  openAgentRunDetail,
  openAgentRunList,
  resetAgentRunPresentationStoreForTests,
} from "../../state/agent-run-presentation-store";
import { AgentRunDrawer } from "../AgentRunDrawer";
import {
  buildAgentAssignment,
  buildAgentResultProjection,
} from "../../test-data";

const TASK_ID = 71201;

afterEach(() => {
  cleanup();
  resetAgentRunStoreForTests();
  resetAgentRunPresentationStoreForTests();
});

function assignment(overrides: Partial<AgentAssignment> = {}): AgentAssignment {
  return buildAgentAssignment({
    task_id: TASK_ID,
    assignment_id: "assignment-reviewer-run-1",
    agent_run_id: "reviewer-run-1",
    agent_id: "reviewer",
    agent_kind: "review",
    conversation_id: "conversation-1",
    parent_turn_id: "turn-parent",
    objective: "Review the generated artifacts.",
    targets: [],
    suggested_capabilities: ["artifact_review"],
    scope_summary: "Generated artifact audit",
    ...overrides,
  });
}

function reviewerLifecycle(): AgentRunLifecycleProjection {
  return {
    agent_run_id: "reviewer-run-1",
    agent_id: "reviewer",
    agent_kind: "review",
    agent_display_name: "Reviewer",
    agent_icon_key: "reviewer",
    status: "completed",
    lifecycle_version: 1,
    task_id: TASK_ID,
    conversation_id: "conversation-1",
    parent_turn_id: "turn-parent",
    parent_run_id: "parent-run-1",
    assignment: assignment(),
    result: buildAgentResultProjection({
      assignment: assignment(),
      agent_display_name: "Reviewer",
      summary: "Artifacts passed review.",
      key_findings: ["No missing outputs."],
      evidence_refs: [],
      tools_used: [],
      limitations: ["Runtime smoke testing was outside the review scope."],
      recommended_next_steps: ["Publish the reviewed artifacts."],
    }),
    safe_error: null,
  };
}

describe("AgentRunDrawer", () => {
  it("renders only the terminal handoff as the final subagent message", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, reviewerLifecycle(), 10);
    openAgentRunDetail(TASK_ID, "parent-run-1", "reviewer-run-1");

    render(
      <AgentRunDrawer
        taskId={TASK_ID}
        activityMessages={[]}
        canStopRuns
        onStopRun={vi.fn()}
      />,
    );

    const finalMessage = screen.getByRole("article", {
      name: "Reviewer final message",
    });
    expect(finalMessage.textContent).toContain("Artifacts passed review.");
    expect(finalMessage.textContent).not.toContain("No missing outputs.");
    expect(finalMessage.textContent).not.toContain(
      "Runtime smoke testing was outside the review scope.",
    );
    expect(finalMessage.textContent).not.toContain(
      "Publish the reviewed artifacts.",
    );
    expect(screen.queryByText("Result")).toBeNull();
    expect(screen.queryByText("Completed")).toBeNull();
  });

  it("renders list and detail identity for a synthetic second agent without Pathfinder branches", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, reviewerLifecycle(), 10);
    openAgentRunList(TASK_ID, "parent-run-1");

    const onStopRun = vi.fn();
    const { container, rerender } = render(
      <AgentRunDrawer
        taskId={TASK_ID}
        activityMessages={[]}
        canStopRuns
        onStopRun={onStopRun}
      />,
    );

    expect(screen.getByTestId("agent-run-drawer")).toBeTruthy();
    expect(screen.getByText("Reviewer")).toBeTruthy();
    expect(
      container.querySelector(
        '[data-agent-id="reviewer"][data-agent-icon-key="reviewer"]',
      ),
    ).not.toBeNull();

    openAgentRunDetail(TASK_ID, "parent-run-1", "reviewer-run-1");
    rerender(
      <AgentRunDrawer
        taskId={TASK_ID}
        activityMessages={[]}
        canStopRuns
        onStopRun={onStopRun}
      />,
    );

    expect(screen.getByTestId("agent-run-detail")).toBeTruthy();
    expect(
      container.querySelector(
        '[data-agent-id="reviewer"][data-agent-icon-key="reviewer"]',
      ),
    ).not.toBeNull();
  });
});
