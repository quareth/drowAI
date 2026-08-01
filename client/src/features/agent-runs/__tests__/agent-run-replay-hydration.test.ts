/**
 * Regression tests for Pathfinder agent-run replay hydration.
 *
 * Covers bounded task replay filtering, cursor advancement, main-card
 * reconstruction, and stale process-local run reconciliation.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  hydrateAgentRunsFromRecentReplay,
  hydrateAgentRunStoreFromReplayItems,
} from "../services/agent-run-replay-hydration";
import {
  applyAgentRunLifecycleUpdate,
  getAgentRunSnapshot,
  MAX_AGENT_RUN_TASK_STATES,
  resetAgentRunStoreForTests,
} from "../state/agent-stream-store";
import {
  closeAgentRunDrawer,
  getAgentRunPresentationSnapshot,
  resetAgentRunPresentationStoreForTests,
} from "../state/agent-run-presentation-store";
import { clearTaskState, getTaskStreamSnapshot } from "@/state/chat-stream-store";
import type { AgentAssignment, AgentRunLifecycleProjection } from "../contracts/agent-run";
import {
  buildAgentAssignment,
  buildAgentRuntimeIdentity,
} from "../test-data";
import type { StreamEvent } from "@/types/packets";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

const TASK_ID = 62101;

afterEach(() => {
  vi.clearAllMocks();
  resetAgentRunStoreForTests();
  resetAgentRunPresentationStoreForTests();
  clearTaskState(TASK_ID);
});

function assignment(): AgentAssignment {
  return buildAgentAssignment({
    runtimeIdentity: buildAgentRuntimeIdentity({
      task_id: TASK_ID,
      workspace_id: "workspace-1",
      runtime_placement_mode: "local",
      actor_id: "user-1",
      runner_id: null,
      execution_site_id: null,
      provider: null,
      model: null,
      reasoning_effort: null,
    }),
    assignment_id: "assign-pathfinder-1",
    agent_run_id: "pathfinder-run-1",
    conversation_id: "conv-pathfinder",
    parent_turn_id: "turn-parent",
    parent_graph_thread_id: "thread-parent",
    objective: "Enumerate exposed services",
    targets: ["10.0.0.5"],
    suggested_capabilities: ["port_scan"],
    relevant_context: {},
  });
}

function lifecycle(
  overrides: Partial<AgentRunLifecycleProjection> = {},
): AgentRunLifecycleProjection {
  return {
    agent_run_id: "pathfinder-run-1",
    agent_id: "pathfinder",
    agent_kind: "recon",
    agent_display_name: "Pathfinder",
    agent_icon_key: "pathfinder",
    status: "running",
    lifecycle_version: 1,
    task_id: TASK_ID,
    conversation_id: "conv-pathfinder",
    parent_turn_id: "turn-parent",
    parent_run_id: "parent-run-1",
    assignment: assignment(),
    result: null,
    safe_error: null,
    ...overrides,
  };
}

function lifecyclePacket(
  sequence: number,
  overrides: Partial<AgentRunLifecycleProjection> = {},
): StreamEvent {
  const projection = lifecycle(overrides);
  return {
    type: "status",
    content: "agent_run_lifecycle",
    task_id: TASK_ID,
    sequence,
    metadata: {
      subtype: "agent_run_lifecycle",
      producer_type: "subagent",
      agent_run_id: projection.agent_run_id,
      agent_id: projection.agent_id,
      agent_kind: projection.agent_kind,
      agent_display_name: projection.agent_display_name,
      agent_icon_key: projection.agent_icon_key,
      parent_turn_id: projection.parent_turn_id,
      parent_run_id: projection.parent_run_id,
      internal_only: false,
      lifecycle_version: projection.lifecycle_version,
      sequence,
    },
    agent_run: projection,
  } as StreamEvent;
}

function activityPacket(sequence: number): StreamEvent {
  return {
    type: "reasoning_delta",
    content: "internal Pathfinder reasoning",
    task_id: TASK_ID,
    sequence,
    metadata: {
      id: "child-turn",
      ind: 0,
      step_type: "reasoning_delta",
      reasoning_section_id: "child-turn:reasoning:0",
      turn_sequence: 1,
      producer_type: "subagent",
      agent_run_id: "pathfinder-run-1",
      agent_id: "pathfinder",
      agent_kind: "recon",
      agent_display_name: "Pathfinder",
      parent_turn_id: "turn-parent",
      parent_run_id: "parent-run-1",
      internal_only: false,
      sequence,
    },
  };
}

describe("agent-run replay hydration", () => {
  it("hydrates only Pathfinder replay packets and keeps the drawer closed", () => {
    const result = hydrateAgentRunStoreFromReplayItems(TASK_ID, [
      {
        type: "assistant_message",
        content: "main chat",
        task_id: TASK_ID,
        sequence: 10,
        metadata: { sequence: 10 },
      },
      lifecyclePacket(11),
      activityPacket(12),
    ], 12);

    const snapshot = getAgentRunSnapshot(TASK_ID);
    expect(result.replayedPackets).toBe(2);
    expect(result.lastSequence).toBe(12);
    expect(snapshot.runs).toHaveLength(1);
    expect(snapshot.runsById["pathfinder-run-1"].activity).toHaveLength(1);
    expect(snapshot.runsById["pathfinder-run-1"].agentIconKey).toBe("pathfinder");
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toEqual({
      isOpen: false,
      parentRunId: null,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });
    expect(getTaskStreamSnapshot(TASK_ID).items).toHaveLength(2);
    expect(
      getTaskStreamSnapshot(TASK_ID).items.some(
        item => item.type === "status" && item.content === "agent_run_lifecycle",
      ),
    ).toBe(true);
    expect(
      getTaskStreamSnapshot(TASK_ID).items.some(item => item.content === "internal Pathfinder reasoning"),
    ).toBe(true);
    expect(getTaskStreamSnapshot(TASK_ID).lastSequence).toBe(12);
  });

  it("hydrates a synthetic second-agent icon key from lifecycle metadata", () => {
    hydrateAgentRunStoreFromReplayItems(TASK_ID, [
      lifecyclePacket(13, {
        agent_run_id: "review-run-1",
        agent_id: "reviewer",
        agent_kind: "review",
        agent_display_name: "Reviewer",
        agent_icon_key: "reviewer",
        assignment: null,
      }),
    ], 13);

    expect(getAgentRunSnapshot(TASK_ID).runsById["review-run-1"]).toMatchObject({
      agentId: "reviewer",
      agentDisplayName: "Reviewer",
      agentIconKey: "reviewer",
    });
  });

  it("rehydrates a task after bounded task-state eviction", () => {
    hydrateAgentRunStoreFromReplayItems(TASK_ID, [lifecyclePacket(1)], 1);
    for (let index = 1; index <= MAX_AGENT_RUN_TASK_STATES; index += 1) {
      const taskId = 70000 + index;
      applyAgentRunLifecycleUpdate(
        taskId,
        lifecycle({
          agent_run_id: `other-run-${index}`,
          task_id: taskId,
          conversation_id: `other-conversation-${index}`,
          parent_turn_id: `other-turn-${index}`,
          parent_run_id: `other-parent-${index}`,
          assignment: {
            ...assignment(),
            assignment_id: `other-assignment-${index}`,
            agent_run_id: `other-run-${index}`,
            task_id: taskId,
            conversation_id: `other-conversation-${index}`,
            parent_turn_id: `other-turn-${index}`,
            runtime_identity: {
              ...assignment().runtime_identity,
              task_id: taskId,
              workspace_id: `workspace-${taskId}`,
            },
          },
        }),
      );
    }
    expect(getAgentRunSnapshot(TASK_ID).runs).toHaveLength(0);

    const replayResult = hydrateAgentRunStoreFromReplayItems(
      TASK_ID,
      [lifecyclePacket(2)],
      2,
    );

    expect(replayResult.replayedPackets).toBe(1);
    expect(getAgentRunSnapshot(TASK_ID).runsById["pathfinder-run-1"]).toBeDefined();
  });

  it("marks a replayed nonterminal Pathfinder run interrupted when local status is empty", async () => {
    closeAgentRunDrawer(TASK_ID);
    mocked.apiFetch
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [lifecyclePacket(21), activityPacket(22)],
            nextAfter: 22,
            hasMore: false,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            process_local: true,
            task_id: TASK_ID,
            agent_runs: [],
          }),
          { status: 200 },
        ),
      );

    const result = await hydrateAgentRunsFromRecentReplay(TASK_ID);

    expect(mocked.apiFetch).toHaveBeenNthCalledWith(
      1,
      "/api/tasks/62101/reasoning/replay?after=0&limit=200",
      expect.objectContaining({ method: "GET" }),
    );
    expect(mocked.apiFetch).toHaveBeenNthCalledWith(
      2,
      "/api/tasks/62101/agent-runs/local",
      expect.objectContaining({ method: "GET" }),
    );
    expect(result).toMatchObject({
      replayedPackets: 2,
      localStatusReconciled: true,
      lastSequence: 22,
    });

    const snapshot = getAgentRunSnapshot(TASK_ID);
    expect(snapshot.runsById["pathfinder-run-1"].status).toBe("interrupted");
    expect(snapshot.runsById["pathfinder-run-1"].safeError).toContain(
      "current backend process no longer owns it",
    );
    expect(getAgentRunPresentationSnapshot(TASK_ID).isOpen).toBe(false);
    expect(getTaskStreamSnapshot(TASK_ID).items).toHaveLength(2);
    expect(
      getTaskStreamSnapshot(TASK_ID).items.some(
        item => item.content === "agent_run_lifecycle",
      ),
    ).toBe(true);
  });

  it("paginates replay so long refresh recovers early activity and terminal lifecycle", async () => {
    const filler = Array.from({ length: 198 }, (_, index) => ({
      type: "assistant_message",
      content: `main ${index}`,
      task_id: TASK_ID,
      sequence: 23 + index,
      metadata: { sequence: 23 + index },
    }));
    mocked.apiFetch
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [lifecyclePacket(21), activityPacket(22), ...filler],
            nextAfter: 220,
            hasMore: true,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [
              lifecyclePacket(221, {
                status: "completed",
                lifecycle_version: 2,
                assignment: null,
                result: {
                  agent_run_id: "pathfinder-run-1",
                  agent_id: "pathfinder",
                  agent_kind: "recon",
                  agent_display_name: "Pathfinder",
                  outcome: "completed",
                  summary: "Ports mapped.",
                  key_findings: ["80/tcp open"],
                  evidence_refs: [],
                  tools_used: ["nmap"],
                  limitations: [],
                  recommended_next_steps: [],
                },
              }),
            ],
            nextAfter: 221,
            hasMore: false,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            process_local: true,
            task_id: TASK_ID,
            agent_runs: [],
          }),
          { status: 200 },
        ),
      );

    const result = await hydrateAgentRunsFromRecentReplay(TASK_ID);

    expect(mocked.apiFetch).toHaveBeenNthCalledWith(
      1,
      "/api/tasks/62101/reasoning/replay?after=0&limit=200",
      expect.objectContaining({ method: "GET" }),
    );
    expect(mocked.apiFetch).toHaveBeenNthCalledWith(
      2,
      "/api/tasks/62101/reasoning/replay?after=220&limit=200",
      expect.objectContaining({ method: "GET" }),
    );
    expect(result).toMatchObject({
      replayedPackets: 3,
      localStatusReconciled: true,
      lastSequence: 221,
    });

    const run = getAgentRunSnapshot(TASK_ID).runsById["pathfinder-run-1"];
    expect(run.status).toBe("completed");
    expect(run.activity.map(entry => entry.sequence)).toEqual([22]);
    expect(getTaskStreamSnapshot(TASK_ID).items.some(item => item.content === "internal Pathfinder reasoning")).toBe(true);
    expect(getTaskStreamSnapshot(TASK_ID).lastSequence).toBe(221);
  });
});
