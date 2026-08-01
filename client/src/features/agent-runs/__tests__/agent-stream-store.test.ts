/**
 * This test-only module verifies the agent-run store's lifecycle merging,
 * bounded activity retention, local-status reconciliation, and data retention.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  applyAgentRunActivityPayload,
  applyAgentRunLifecyclePayload,
  applyAgentRunLifecycleUpdate,
  getAgentRunSnapshot,
  MAX_AGENT_RUN_ACTIVITY_EVENTS,
  MAX_AGENT_RUN_TASK_STATES,
  MAX_AGENT_RUNS_PER_TASK,
  reconcileAgentRunsWithLocalStatus,
  resetAgentRunStoreForTests,
} from "../state/agent-stream-store";
import { resolveAgentDisplayName } from "../contracts/agent-run";
import type {
  AgentAssignment,
  AgentRunLifecycleProjection,
  LocalAgentRunStatusProjection,
} from "../contracts/agent-run";
import type { StreamEvent } from "@/types/packets";
import {
  buildAgentAssignment,
  buildAgentRuntimeIdentity,
} from "../test-data";

const TASK_ID = 51101;
const OTHER_TASK_ID = 51102;

afterEach(() => {
  resetAgentRunStoreForTests();
});

function assignment(overrides: Partial<AgentAssignment> = {}): AgentAssignment {
  const { runtime_identity, task_id, tenant_id, ...assignmentOverrides } = overrides;
  return buildAgentAssignment({
    runtimeIdentity: buildAgentRuntimeIdentity({
      task_id: task_id ?? TASK_ID,
      tenant_id: tenant_id ?? 77,
      workspace_id: "workspace-1",
      actor_id: "user-1",
      ...runtime_identity,
    }),
    assignment_id: "assign-1",
    conversation_id: "conv-1",
    parent_graph_thread_id: "thread-parent",
    objective: "Map exposed services",
    suggested_capabilities: ["port_scan"],
    scope_summary: "service discovery",
    relevant_context: {},
    ...assignmentOverrides,
  });
}

function lifecycle(
  overrides: Partial<AgentRunLifecycleProjection> = {},
): AgentRunLifecycleProjection {
  return {
    agent_run_id: "run-1",
    agent_id: "pathfinder",
    agent_kind: "recon",
    agent_display_name: "Pathfinder",
    agent_icon_key: "pathfinder",
    status: "queued",
    lifecycle_version: 1,
    task_id: TASK_ID,
    conversation_id: "conv-1",
    parent_turn_id: "turn-1",
    parent_run_id: "parent-run-1",
    assignment: assignment(),
    result: null,
    safe_error: null,
    ...overrides,
  };
}

function scopedLifecycle(
  taskId: number,
  agentRunId: string,
  status: AgentRunLifecycleProjection["status"] = "running",
): AgentRunLifecycleProjection {
  return lifecycle({
    agent_run_id: agentRunId,
    status,
    task_id: taskId,
    conversation_id: `conv-${taskId}`,
    parent_turn_id: `turn-${taskId}`,
    parent_run_id: `parent-${taskId}`,
    assignment: assignment({
      assignment_id: `assignment-${agentRunId}`,
      agent_run_id: agentRunId,
      task_id: taskId,
      conversation_id: `conv-${taskId}`,
      parent_turn_id: `turn-${taskId}`,
      runtime_identity: {
        ...assignment().runtime_identity,
        task_id: taskId,
        workspace_id: `workspace-${taskId}`,
      },
    }),
  });
}

function lifecycleEvent(
  projection: AgentRunLifecycleProjection,
  sequence: number,
): StreamEvent {
  return {
    type: "status",
    content: "agent_run_lifecycle",
    sequence,
    task_id: projection.task_id,
    metadata: {
      subtype: "agent_run_lifecycle",
      producer_type: "subagent",
      agent_run_id: projection.agent_run_id,
      agent_id: projection.agent_id,
      agent_kind: "recon",
      agent_display_name: "Pathfinder",
      agent_icon_key: projection.agent_icon_key,
      parent_turn_id: projection.parent_turn_id,
      parent_run_id: projection.parent_run_id,
      lifecycle_version: projection.lifecycle_version,
      internal_only: false,
      sequence,
    },
    agent_run: projection,
  } as StreamEvent;
}

function activityEvent(sequence: number, agentRunId = "run-1"): StreamEvent {
  return {
    type: "tool_start",
    content: `tool ${sequence}`,
    sequence,
    task_id: TASK_ID,
    metadata: {
      producer_type: "subagent",
      agent_run_id: agentRunId,
      agent_id: "pathfinder",
      agent_kind: "recon",
      agent_display_name: "Pathfinder",
      agent_icon_key: "pathfinder",
      parent_turn_id: "turn-1",
      parent_run_id: "parent-run-1",
      tool_call_id: `tool-${sequence}`,
      sequence,
    },
  };
}

describe("agent-stream-store lifecycle state", () => {
  it("uses reported display names and generic unknown-id fallback only", () => {
    expect(resolveAgentDisplayName("reviewer", "Reviewer")).toBe("Reviewer");
    expect(resolveAgentDisplayName("unknown_agent", null)).toBe("Unknown Agent");
  });

  it("merges lifecycle updates by monotonic version and preserves assignment", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 10);
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        status: "completed",
        lifecycle_version: 2,
        assignment: null,
        result: {
          agent_run_id: "run-1",
          agent_id: "pathfinder",
          agent_kind: "recon",
          agent_display_name: "Pathfinder",
          outcome: "completed",
          summary: "Found SSH and HTTPS",
          key_findings: ["SSH open"],
          evidence_refs: [{ tool_call_id: "tool-2" }],
          tools_used: ["nmap"],
          limitations: [],
          recommended_next_steps: ["Review service banners"],
          final_checkpoint_id: "checkpoint-1",
        },
      }),
      20,
    );
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({ status: "running", lifecycle_version: 1 }),
      30,
    );

    const run = getAgentRunSnapshot(TASK_ID).runsById["run-1"];
    expect(run.status).toBe("completed");
    expect(run.lifecycleVersion).toBe(2);
    expect(run.assignment?.objective).toBe("Map exposed services");
    expect(run.result?.summary).toBe("Found SSH and HTTPS");
    expect(run.firstSequence).toBe(10);
    expect(run.lastSequence).toBe(20);
  });

  it("keeps state keyed by task and agent_run_id", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 1);
    applyAgentRunLifecycleUpdate(
      OTHER_TASK_ID,
      lifecycle({
        task_id: OTHER_TASK_ID,
        conversation_id: "conv-2",
        parent_turn_id: "turn-2",
        parent_run_id: "parent-run-2",
        assignment: assignment({
          task_id: OTHER_TASK_ID,
          conversation_id: "conv-2",
          parent_turn_id: "turn-2",
          runtime_identity: {
            ...assignment().runtime_identity,
            task_id: OTHER_TASK_ID,
          },
        }),
      }),
      2,
    );

    expect(getAgentRunSnapshot(TASK_ID).runs).toHaveLength(1);
    expect(getAgentRunSnapshot(OTHER_TASK_ID).runs).toHaveLength(1);
    expect(getAgentRunSnapshot(TASK_ID).runs[0].conversationId).toBe("conv-1");
    expect(getAgentRunSnapshot(OTHER_TASK_ID).runs[0].conversationId).toBe("conv-2");
  });

  it("orders runs by first task sequence and keeps same-task conversations separate", () => {
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        agent_run_id: "run-late",
        conversation_id: "conv-late",
        parent_turn_id: "turn-late",
        parent_run_id: "parent-run-late",
        assignment: assignment({
          agent_run_id: "run-late",
          conversation_id: "conv-late",
          parent_turn_id: "turn-late",
          objective: "Later conversation",
        }),
      }),
      20,
    );
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        agent_run_id: "run-early",
        conversation_id: "conv-early",
        parent_turn_id: "turn-early",
        parent_run_id: "parent-run-early",
        assignment: assignment({
          agent_run_id: "run-early",
          conversation_id: "conv-early",
          parent_turn_id: "turn-early",
          objective: "Earlier conversation",
        }),
      }),
      10,
    );

    const snapshot = getAgentRunSnapshot(TASK_ID);
    expect(snapshot.runs.map(run => run.agentRunId)).toEqual(["run-early", "run-late"]);
    expect(snapshot.runsById["run-early"].conversationId).toBe("conv-early");
    expect(snapshot.runsById["run-late"].conversationId).toBe("conv-late");
  });

  it("hydrates lifecycle payloads into the task-scoped data snapshot", () => {
    applyAgentRunLifecyclePayload(TASK_ID, lifecycleEvent(lifecycle(), 5));

    const snapshot = getAgentRunSnapshot(TASK_ID);
    expect(snapshot.runs).toHaveLength(1);
    expect(snapshot.runsById["run-1"].firstSequence).toBe(5);
  });

  it("accepts any backend-attributed subagent kind without rewriting its identity", () => {
    const projection = {
      ...lifecycle({ assignment: null }),
      agent_run_id: "review-run-1",
      agent_id: "reviewer",
      agent_kind: "review",
      agent_display_name: "Reviewer",
      agent_icon_key: "reviewer",
    } as AgentRunLifecycleProjection;
    const event = lifecycleEvent(projection, 8);
    event.metadata = {
      ...event.metadata,
      agent_run_id: projection.agent_run_id,
      agent_id: projection.agent_id,
      agent_kind: projection.agent_kind,
      agent_display_name: projection.agent_display_name,
      agent_icon_key: "reviewer",
    };

    expect(applyAgentRunLifecyclePayload(TASK_ID, event)).toBe(true);
    expect(getAgentRunSnapshot(TASK_ID).runsById["review-run-1"]).toMatchObject({
      agentKind: "review",
      agentDisplayName: "Reviewer",
      agentIconKey: "reviewer",
      firstSequence: 8,
    });
  });

  it("marks replayed nonterminal runs interrupted when absent from local status", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle({ status: "running" }), 10);

    reconcileAgentRunsWithLocalStatus(TASK_ID, []);

    const snapshot = getAgentRunSnapshot(TASK_ID);
    const run = snapshot.runsById["run-1"];
    expect(run.status).toBe("interrupted");
    expect(run.lifecycleVersion).toBe(1);
    expect(run.safeError).toContain("current backend process no longer owns it");
  });

  it("overlays matching local status without downgrading newer lifecycle versions", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle({ status: "running", lifecycle_version: 3 }), 10);

    reconcileAgentRunsWithLocalStatus(TASK_ID, [
      {
        ...lifecycle({ status: "queued", lifecycle_version: 2 }),
        assignment: assignment(),
        cancel_requested: false,
        created_at: "2026-01-01T00:00:00Z",
        started_at: null,
        completed_at: null,
      } satisfies LocalAgentRunStatusProjection,
    ]);

    const run = getAgentRunSnapshot(TASK_ID).runsById["run-1"];
    expect(run.status).toBe("running");
    expect(run.lifecycleVersion).toBe(3);
  });
});

describe("agent-stream-store activity state", () => {
  it("orders and dedupes activity by task stream sequence", () => {
    applyAgentRunActivityPayload(TASK_ID, activityEvent(3));
    applyAgentRunActivityPayload(TASK_ID, activityEvent(1));
    applyAgentRunActivityPayload(TASK_ID, activityEvent(2));
    applyAgentRunActivityPayload(TASK_ID, activityEvent(2));

    const run = getAgentRunSnapshot(TASK_ID).runsById["run-1"];
    expect(run.activity.map(entry => entry.sequence)).toEqual([1, 2, 3]);
    expect(run.firstSequence).toBe(1);
    expect(run.lastSequence).toBe(3);
  });

  it("keeps only a bounded activity window per run", () => {
    for (let sequence = 1; sequence <= MAX_AGENT_RUN_ACTIVITY_EVENTS + 5; sequence += 1) {
      applyAgentRunActivityPayload(TASK_ID, activityEvent(sequence));
    }

    const activity = getAgentRunSnapshot(TASK_ID).runsById["run-1"].activity;
    expect(activity).toHaveLength(MAX_AGENT_RUN_ACTIVITY_EVENTS);
    expect(activity[0].sequence).toBe(6);
    expect(activity.at(-1)?.sequence).toBe(MAX_AGENT_RUN_ACTIVITY_EVENTS + 5);
  });

  it("dedupes before applying the bounded activity window", () => {
    for (let sequence = 1; sequence <= MAX_AGENT_RUN_ACTIVITY_EVENTS + 5; sequence += 1) {
      applyAgentRunActivityPayload(TASK_ID, activityEvent(sequence));
      applyAgentRunActivityPayload(TASK_ID, activityEvent(sequence));
    }

    const activity = getAgentRunSnapshot(TASK_ID).runsById["run-1"].activity;
    expect(activity).toHaveLength(MAX_AGENT_RUN_ACTIVITY_EVENTS);
    expect(activity.map(entry => entry.sequence)).toEqual(
      Array.from({ length: MAX_AGENT_RUN_ACTIVITY_EVENTS }, (_, index) => index + 6),
    );
  });

  it("ignores internal-only child activity", () => {
    applyAgentRunActivityPayload(TASK_ID, {
      ...activityEvent(1),
      metadata: {
        ...activityEvent(1).metadata,
        internal_only: true,
      },
    });

    expect(getAgentRunSnapshot(TASK_ID).runs).toHaveLength(0);
  });
});

describe("agent-stream-store bounded retention", () => {
  it("retains only the most recently accessed task states", () => {
    for (let taskId = 1; taskId <= MAX_AGENT_RUN_TASK_STATES; taskId += 1) {
      applyAgentRunLifecycleUpdate(taskId, scopedLifecycle(taskId, `run-${taskId}`));
    }
    getAgentRunSnapshot(1);

    const addedTaskId = MAX_AGENT_RUN_TASK_STATES + 1;
    applyAgentRunLifecycleUpdate(
      addedTaskId,
      scopedLifecycle(addedTaskId, `run-${addedTaskId}`),
    );

    expect(getAgentRunSnapshot(1).runs).toHaveLength(1);
    expect(getAgentRunSnapshot(2).runs).toHaveLength(0);
    expect(getAgentRunSnapshot(addedTaskId).runs).toHaveLength(1);
  });

  it("evicts closed terminal-only tasks before active task state", () => {
    applyAgentRunLifecycleUpdate(1, scopedLifecycle(1, "run-1", "completed"));
    for (let taskId = 2; taskId <= MAX_AGENT_RUN_TASK_STATES; taskId += 1) {
      applyAgentRunLifecycleUpdate(taskId, scopedLifecycle(taskId, `run-${taskId}`));
    }
    getAgentRunSnapshot(1);

    const addedTaskId = MAX_AGENT_RUN_TASK_STATES + 1;
    applyAgentRunLifecycleUpdate(
      addedTaskId,
      scopedLifecycle(addedTaskId, `run-${addedTaskId}`),
    );

    expect(getAgentRunSnapshot(1).runs).toHaveLength(0);
    expect(getAgentRunSnapshot(2).runs).toHaveLength(1);
  });

  it("evicts terminal runs before active runs at the per-task cap", () => {
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      scopedLifecycle(TASK_ID, "run-terminal", "completed"),
    );
    for (let index = 1; index < MAX_AGENT_RUNS_PER_TASK; index += 1) {
      applyAgentRunLifecycleUpdate(
        TASK_ID,
        scopedLifecycle(TASK_ID, `run-${index}`),
      );
    }

    applyAgentRunLifecycleUpdate(
      TASK_ID,
      scopedLifecycle(TASK_ID, `run-${MAX_AGENT_RUNS_PER_TASK}`),
    );

    const snapshot = getAgentRunSnapshot(TASK_ID);
    expect(snapshot.runs).toHaveLength(MAX_AGENT_RUNS_PER_TASK);
    expect(snapshot.runsById["run-terminal"]).toBeUndefined();
    expect(snapshot.runsById[`run-${MAX_AGENT_RUNS_PER_TASK}`]).toBeDefined();
  });

  it("refreshes run recency when a run is mutated", () => {
    for (let index = 1; index <= MAX_AGENT_RUNS_PER_TASK; index += 1) {
      applyAgentRunLifecycleUpdate(
        TASK_ID,
        scopedLifecycle(TASK_ID, `run-${index}`),
      );
    }
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      {
        ...scopedLifecycle(TASK_ID, "run-1", "running"),
        lifecycle_version: 2,
      },
    );

    applyAgentRunLifecycleUpdate(
      TASK_ID,
      scopedLifecycle(TASK_ID, `run-${MAX_AGENT_RUNS_PER_TASK + 1}`),
    );

    const snapshot = getAgentRunSnapshot(TASK_ID);
    expect(snapshot.runsById["run-1"]).toBeDefined();
    expect(snapshot.runsById["run-2"]).toBeUndefined();
  });

});
