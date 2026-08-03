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
import {
  buildAgentAssignment,
  buildAgentResultProjection,
} from "../test-data";

const TASK_ID = 73101;

describe("agent-run test data", () => {
  it("derives linked identities and returns fresh nested values", () => {
    const firstAssignment = buildAgentAssignment({ task_id: TASK_ID });
    const secondAssignment = buildAgentAssignment({ task_id: TASK_ID });
    const firstResult = buildAgentResultProjection({ assignment: firstAssignment });
    const secondResult = buildAgentResultProjection({ assignment: secondAssignment });

    expect(firstAssignment).toMatchObject({ task_id: TASK_ID });
    expect(firstAssignment.targets).not.toBe(secondAssignment.targets);
    expect(firstResult).toMatchObject({
      agent_run_id: firstAssignment.agent_run_id,
      agent_id: firstAssignment.agent_id,
      agent_kind: firstAssignment.agent_kind,
    });
    expect(firstResult.evidence_refs).not.toBe(secondResult.evidence_refs);
  });
});

function assignment(
  overrides: Partial<AgentAssignment> = {},
): AgentAssignment {
  return buildAgentAssignment({
    task_id: TASK_ID,
    assignment_id: "assignment-1",
    objective: "Map exposed services.",
    targets: ["10.0.0.8"],
    suggested_capabilities: ["port_scan"],
    scope_summary: "Approved target only.",
    ...overrides,
  });
}

function result(
  overrides: Partial<AgentResultProjection> = {},
): AgentResultProjection {
  const { agent_run_id, agent_id, agent_kind, ...resultOverrides } = overrides;
  return buildAgentResultProjection({
    assignment: assignment({
      agent_run_id: agent_run_id ?? "run-1",
      agent_id: agent_id ?? "pathfinder",
      agent_kind: agent_kind ?? "recon",
    }),
    agent_display_name: "Pathfinder",
    summary: "Mapped the approved target.",
    key_findings: ["80/tcp open"],
    evidence_refs: [{ tool_call_id: "tool-1", artifact_id: "artifact-1" }],
    recommended_next_steps: ["Review HTTP headers."],
    ...resultOverrides,
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
  it("accepts only the UI-safe assignment projection", () => {
    const safeAssignment = assignment();

    expect(readAgentAssignment(safeAssignment)).toEqual(safeAssignment);
    expect(
      readAgentAssignment({
        ...safeAssignment,
        runtime_identity: {
          workspace_path: "/host/task-73101",
          runner_id: "runner-1",
          credential_ref: { credential_id: "credential-1" },
        },
      }),
    ).toBeNull();
    expect(readAgentResultProjection(result())).toEqual(result());
  });

  it("rejects malformed objectives, result fields, and arrays", () => {
    expect(readAgentAssignment({ ...assignment(), objective: 42 })).toBeNull();
    expect(readAgentAssignment({ ...assignment(), targets: "10.0.0.8" })).toBeNull();
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
