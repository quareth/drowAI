import { afterEach, describe, expect, it } from "vitest";

import {
  applyAgentRunActivityPayload,
  applyAgentRunLifecyclePayload,
  applyAgentRunLifecycleUpdate,
  closeAgentRunDrawer,
  getAgentRunSnapshot,
  MAX_AGENT_RUN_ACTIVITY_EVENTS,
  openAgentRunDetail,
  openAgentRunList,
  reconcileAgentRunsWithLocalStatus,
  resetAgentRunStoreForTests,
  returnAgentRunDrawerToList,
  setAgentRunActivityExpanded,
} from "../state/agent-stream-store";
import type {
  AgentAssignment,
  AgentRunLifecycleProjection,
  LocalAgentRunStatusProjection,
} from "../contracts/agent-run";
import type { StreamEvent } from "@/types/packets";

const TASK_ID = 51101;
const OTHER_TASK_ID = 51102;

afterEach(() => {
  resetAgentRunStoreForTests();
});

function assignment(overrides: Partial<AgentAssignment> = {}): AgentAssignment {
  return {
    assignment_id: "assign-1",
    agent_run_id: "run-1",
    agent_kind: "recon",
    task_id: TASK_ID,
    tenant_id: 77,
    conversation_id: "conv-1",
    parent_turn_id: "turn-1",
    parent_graph_thread_id: "thread-parent",
    objective: "Map exposed services",
    targets: ["10.0.0.10"],
    suggested_capabilities: ["port_scan"],
    scope_summary: "service discovery",
    relevant_context: {},
    runtime_identity: {
      tenant_id: 77,
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

function lifecycle(
  overrides: Partial<AgentRunLifecycleProjection> = {},
): AgentRunLifecycleProjection {
  return {
    agent_run_id: "run-1",
    agent_kind: "recon",
    agent_display_name: "Scout",
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
      agent_kind: "recon",
      agent_display_name: "Scout",
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
      agent_kind: "recon",
      agent_display_name: "Scout",
      parent_turn_id: "turn-1",
      parent_run_id: "parent-run-1",
      tool_call_id: `tool-${sequence}`,
      sequence,
    },
  };
}

describe("agent-stream-store lifecycle state", () => {
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
          agent_kind: "recon",
          agent_display_name: "Scout",
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

  it("hydrates lifecycle payloads without opening drawer presentation state", () => {
    expect(getAgentRunSnapshot(TASK_ID).presentation.isOpen).toBe(false);

    applyAgentRunLifecyclePayload(TASK_ID, lifecycleEvent(lifecycle(), 5));

    const snapshot = getAgentRunSnapshot(TASK_ID);
    expect(snapshot.runs).toHaveLength(1);
    expect(snapshot.presentation).toEqual({
      isOpen: false,
      parentRunId: null,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });
  });

  it("accepts any backend-attributed subagent kind without rewriting its identity", () => {
    const projection = {
      ...lifecycle({ assignment: null }),
      agent_run_id: "review-run-1",
      agent_kind: "review",
      agent_display_name: "Reviewer",
    } as AgentRunLifecycleProjection;
    const event = lifecycleEvent(projection, 8);
    event.metadata = {
      ...event.metadata,
      agent_run_id: projection.agent_run_id,
      agent_kind: projection.agent_kind,
      agent_display_name: projection.agent_display_name,
    };

    expect(applyAgentRunLifecyclePayload(TASK_ID, event)).toBe(true);
    expect(getAgentRunSnapshot(TASK_ID).runsById["review-run-1"]).toMatchObject({
      agentKind: "review",
      agentDisplayName: "Reviewer",
      firstSequence: 8,
    });
  });

  it("marks replayed nonterminal runs interrupted when absent from local status", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle({ status: "running" }), 10);
    openAgentRunList(TASK_ID, "parent-run-1");

    reconcileAgentRunsWithLocalStatus(TASK_ID, []);

    const snapshot = getAgentRunSnapshot(TASK_ID);
    const run = snapshot.runsById["run-1"];
    expect(run.status).toBe("interrupted");
    expect(run.lifecycleVersion).toBe(1);
    expect(run.safeError).toContain("current backend process no longer owns it");
    expect(snapshot.presentation).toMatchObject({
      isOpen: true,
      parentRunId: "parent-run-1",
      view: "list",
      selectedAgentRunId: null,
    });
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

describe("agent-stream-store presentation state", () => {
  it("opens list/detail only through presentation actions and resets expansion", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 1);

    openAgentRunList(TASK_ID, "parent-run-1");
    expect(getAgentRunSnapshot(TASK_ID).presentation).toMatchObject({
      isOpen: true,
      parentRunId: "parent-run-1",
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });

    openAgentRunDetail(TASK_ID, "parent-run-1", "run-1");
    expect(getAgentRunSnapshot(TASK_ID).presentation).toMatchObject({
      isOpen: true,
      view: "detail",
      selectedAgentRunId: "run-1",
      activityExpanded: false,
    });

    setAgentRunActivityExpanded(TASK_ID, true);
    expect(getAgentRunSnapshot(TASK_ID).presentation.activityExpanded).toBe(true);

    returnAgentRunDrawerToList(TASK_ID);
    expect(getAgentRunSnapshot(TASK_ID).presentation).toMatchObject({
      isOpen: true,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });

    closeAgentRunDrawer(TASK_ID);
    expect(getAgentRunSnapshot(TASK_ID).presentation).toEqual({
      isOpen: false,
      parentRunId: null,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });
  });
}
);
