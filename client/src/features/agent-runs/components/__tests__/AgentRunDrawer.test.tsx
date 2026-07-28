// @vitest-environment jsdom
/**
 * Verifies drawer presentation stays driven by generic subagent identity metadata.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentAssignment, AgentRunLifecycleProjection } from "../../contracts/agent-run";
import {
  applyAgentRunLifecycleUpdate,
  openAgentRunDetail,
  openAgentRunList,
  resetAgentRunStoreForTests,
} from "../../state/agent-stream-store";
import { AgentRunDrawer } from "../AgentRunDrawer";

const TASK_ID = 71201;

afterEach(() => {
  cleanup();
  resetAgentRunStoreForTests();
});

function assignment(overrides: Partial<AgentAssignment> = {}): AgentAssignment {
  return {
    assignment_id: "assignment-reviewer-run-1",
    agent_run_id: "reviewer-run-1",
    agent_id: "reviewer",
    agent_kind: "review",
    task_id: TASK_ID,
    tenant_id: 7,
    conversation_id: "conversation-1",
    parent_turn_id: "turn-parent",
    parent_graph_thread_id: "thread-parent",
    objective: "Review the generated artifacts.",
    targets: [],
    suggested_capabilities: ["artifact_review"],
    scope_summary: "Generated artifact audit",
    relevant_context: {},
    runtime_identity: {
      tenant_id: 7,
      task_id: TASK_ID,
      workspace_id: "workspace-1",
      runtime_placement_mode: "runner",
      actor_type: "user",
      actor_id: "user-1",
      feature_flags: {},
    },
    ...overrides,
  };
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
    result: {
      agent_run_id: "reviewer-run-1",
      agent_id: "reviewer",
      agent_kind: "review",
      agent_display_name: "Reviewer",
      outcome: "completed",
      summary: "Artifacts passed review.",
      key_findings: ["No missing outputs."],
      evidence_refs: [],
      tools_used: [],
      limitations: [],
      recommended_next_steps: [],
      final_checkpoint_id: "checkpoint-1",
    },
    safe_error: null,
  };
}

describe("AgentRunDrawer", () => {
  it("renders list and detail identity for a synthetic second agent without Pathfinder branches", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, reviewerLifecycle(), 10);
    openAgentRunList(TASK_ID, "parent-run-1");

    const onStopRun = vi.fn();
    const { container, rerender } = render(
      <AgentRunDrawer
        taskId={TASK_ID}
        activityMessages={[]}
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
