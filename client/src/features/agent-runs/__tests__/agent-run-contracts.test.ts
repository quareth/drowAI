/**
 * Contract tests for complete frontend validation of subagent-run payloads.
 */
import { describe, expect, it } from "vitest";

import {
  isAgentRunActivityPayload,
  isSubagentRunMetadata,
  readAgentAssignment,
  readAgentResultProjection,
  readAgentRunActivityIdentity,
  readAgentRunLifecycleProjection,
  readLocalAgentRuns,
  type AgentAssignment,
  type AgentResultProjection,
  type AgentRunLifecycleProjection,
} from "../contracts/agent-run";

const TASK_ID = 73101;

function assignment(
  overrides: Partial<AgentAssignment> = {},
): AgentAssignment {
  return {
    assignment_id: "assignment-1",
    agent_run_id: "run-1",
    agent_id: "pathfinder",
    agent_kind: "recon",
    task_id: TASK_ID,
    tenant_id: 9,
    conversation_id: "conversation-1",
    parent_turn_id: "turn-1",
    parent_graph_thread_id: "parent-thread-1",
    objective: "Map exposed services.",
    targets: ["10.0.0.8"],
    suggested_capabilities: ["port_scan"],
    scope_summary: "Approved target only.",
    relevant_context: { ticket: "ENG-123", priorities: [1, true, null] },
    runtime_identity: {
      tenant_id: 9,
      task_id: TASK_ID,
      user_id: 4,
      workspace_id: "task-73101",
      workspace_path: "/workspace",
      runtime_placement_mode: "runner",
      actor_type: "user",
      actor_id: "4",
      runner_id: "runner-1",
      execution_site_id: "site-1",
      provider: "openai",
      model: "gpt-5.2-mini",
      reasoning_effort: "medium",
      feature_flags: { subagents: true },
      credential_ref: {
        provider: "openai",
        credential_id: "credential-1",
      },
    },
    ...overrides,
  };
}

function result(
  overrides: Partial<AgentResultProjection> = {},
): AgentResultProjection {
  return {
    agent_run_id: "run-1",
    agent_id: "pathfinder",
    agent_kind: "recon",
    agent_display_name: "Pathfinder",
    outcome: "completed",
    summary: "Mapped the approved target.",
    key_findings: ["80/tcp open"],
    evidence_refs: [{ tool_call_id: "tool-1", artifact_id: "artifact-1" }],
    tools_used: ["nmap"],
    limitations: [],
    recommended_next_steps: ["Review HTTP headers."],
    final_checkpoint_id: "checkpoint-1",
    ...overrides,
  };
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
    status: "completed",
    lifecycle_version: 2,
    task_id: TASK_ID,
    conversation_id: "conversation-1",
    parent_turn_id: "turn-1",
    parent_run_id: "parent-run-1",
    assignment: assignment(),
    result: result(),
    safe_error: null,
    ...overrides,
  };
}

function lifecycleEvent(projection: unknown): Record<string, unknown> {
  return {
    type: "status",
    content: "agent_run_lifecycle",
    sequence: 12,
    metadata: {
      subtype: "agent_run_lifecycle",
      producer_type: "subagent",
      agent_run_id: "run-1",
      agent_id: "pathfinder",
      agent_kind: "recon",
      agent_display_name: "Pathfinder",
      agent_icon_key: "pathfinder",
      parent_turn_id: "turn-1",
      parent_run_id: "parent-run-1",
      lifecycle_version: 2,
    },
    agent_run: projection,
  };
}

describe("agent-run projection readers", () => {
  it("accepts complete assignment and result payloads", () => {
    expect(readAgentAssignment(assignment())).toEqual(assignment());
    expect(readAgentResultProjection(result())).toEqual(result());
  });

  it("rejects malformed objectives, result fields, arrays, and runtime identity", () => {
    expect(readAgentAssignment({ ...assignment(), objective: 42 })).toBeNull();
    expect(readAgentAssignment({ ...assignment(), targets: "10.0.0.8" })).toBeNull();
    expect(
      readAgentAssignment({
        ...assignment(),
        runtime_identity: { ...assignment().runtime_identity, task_id: TASK_ID + 1 },
      }),
    ).toBeNull();
    expect(readAgentResultProjection({ ...result(), key_findings: "80/tcp" })).toBeNull();
    expect(
      readAgentResultProjection({
        ...result(),
        evidence_refs: [{ artifact_id: 17 }],
      }),
    ).toBeNull();
  });

  it("keeps a valid lifecycle card while omitting malformed nested values", () => {
    const projection = lifecycle({
      assignment: { ...assignment(), objective: 42 } as unknown as AgentAssignment,
      result: {
        ...result(),
        recommended_next_steps: "continue",
      } as unknown as AgentResultProjection,
    });

    expect(readAgentRunLifecycleProjection(lifecycleEvent(projection))).toMatchObject({
      agent_run_id: "run-1",
      status: "completed",
      assignment: null,
      result: null,
    });
  });

  it("omits nested projections whose immutable identity does not match", () => {
    const projection = lifecycle({
      assignment: assignment({ agent_run_id: "other-run" }),
      result: result({ agent_id: "other-agent" }),
    });

    expect(readAgentRunLifecycleProjection(lifecycleEvent(projection))).toMatchObject({
      agent_run_id: "run-1",
      assignment: null,
      result: null,
    });
  });

  it("filters malformed local-status rows instead of casting them", () => {
    const valid = {
      ...lifecycle({ parent_run_id: undefined }),
      assignment: assignment(),
      cancel_requested: false,
      created_at: "2026-08-01T09:00:00Z",
      started_at: null,
      completed_at: "2026-08-01T09:01:00Z",
    };
    const malformed = {
      ...valid,
      agent_run_id: "run-2",
      assignment: { ...assignment(), agent_run_id: "run-2", targets: "invalid" },
    };

    expect(
      readLocalAgentRuns(
        {
          process_local: true,
          task_id: TASK_ID,
          agent_runs: [valid, malformed],
        },
        TASK_ID,
      ),
    ).toEqual([valid]);
  });

  it("requires the backend process-local envelope marker", () => {
    expect(
      readLocalAgentRuns({ task_id: TASK_ID, agent_runs: [] }, TASK_ID),
    ).toBeNull();
    expect(
      readLocalAgentRuns(
        { process_local: false, task_id: TASK_ID, agent_runs: [] },
        TASK_ID,
      ),
    ).toBeNull();
    expect(
      readLocalAgentRuns(
        { process_local: true, task_id: TASK_ID, agent_runs: [] },
        TASK_ID,
      ),
    ).toEqual([]);
  });

  it("uses the same lifecycle reader for live events and replay packets", () => {
    const event = lifecycleEvent(lifecycle());
    const replayPacket = {
      placement: { turn_index: 0 },
      obj: event,
      sequence: 12,
      task_id: TASK_ID,
    };

    expect(readAgentRunLifecycleProjection(replayPacket)).toEqual(
      readAgentRunLifecycleProjection(event),
    );
  });

  it("recognizes child activity for any valid agent kind", () => {
    const event = {
      type: "reasoning_delta",
      content: "Correlating evidence",
      metadata: {
        producer_type: "subagent",
        agent_run_id: "run-analysis-1",
        agent_id: "evidence_analyst",
        agent_kind: "analysis",
        agent_display_name: "Evidence Analyst",
        parent_turn_id: "turn-1",
      },
    };

    expect(isSubagentRunMetadata(event.metadata)).toBe(true);
    expect(isAgentRunActivityPayload(event)).toBe(true);
    expect(readAgentRunActivityIdentity(TASK_ID, event)).toMatchObject({
      agentRunId: "run-analysis-1",
      agentId: "evidence_analyst",
      agentKind: "analysis",
      agentDisplayName: "Evidence Analyst",
    });
  });
});
