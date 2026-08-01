/** Fresh linked contract builders shared by frontend agent-run tests. */

import type {
  AgentAssignment,
  AgentResultProjection,
  AgentRuntimeIdentity,
} from "./contracts/agent-run";

export function buildAgentRuntimeIdentity(
  overrides: Partial<AgentRuntimeIdentity> = {},
): AgentRuntimeIdentity {
  const taskId = overrides.task_id ?? 42;
  const credentialRef = overrides.credential_ref;
  return {
    tenant_id: 7,
    task_id: taskId,
    user_id: 3,
    workspace_id: `task-${taskId}`,
    workspace_path: "/workspace",
    runtime_placement_mode: "runner",
    actor_type: "user",
    actor_id: "3",
    runner_id: "runner-1",
    execution_site_id: "site-1",
    provider: "openai",
    model: "gpt-5.2-mini",
    reasoning_effort: "medium",
    ...overrides,
    feature_flags: { ...(overrides.feature_flags ?? {}) },
    credential_ref: credentialRef ? { ...credentialRef } : credentialRef ?? null,
  };
}

interface AgentAssignmentBuilderOptions extends Partial<
  Omit<AgentAssignment, "runtime_identity" | "task_id" | "tenant_id">
> {
  runtimeIdentity?: AgentRuntimeIdentity;
}

export function buildAgentAssignment(
  options: AgentAssignmentBuilderOptions = {},
): AgentAssignment {
  const { runtimeIdentity, ...overrides } = options;
  const identity = buildAgentRuntimeIdentity(runtimeIdentity);
  return {
    assignment_id: "assign-1",
    agent_run_id: "run-1",
    agent_id: "pathfinder",
    agent_kind: "recon",
    conversation_id: "conversation-1",
    parent_turn_id: "turn-1",
    parent_graph_thread_id: "parent-thread-1",
    objective: "Map open services on the approved target.",
    scope_summary: "Approved internal test host only.",
    ...overrides,
    task_id: identity.task_id,
    tenant_id: identity.tenant_id,
    targets: [...(overrides.targets ?? ["10.0.0.10"])],
    suggested_capabilities: [
      ...(overrides.suggested_capabilities ?? ["host_discovery", "port_scan"]),
    ],
    relevant_context: structuredClone(
      overrides.relevant_context ?? { ticket: "ENG-123" },
    ),
    runtime_identity: identity,
  };
}

interface AgentResultProjectionBuilderOptions
  extends Partial<
    Omit<AgentResultProjection, "agent_run_id" | "agent_id" | "agent_kind">
  > {
  assignment: AgentAssignment;
}

export function buildAgentResultProjection({
  assignment,
  ...overrides
}: AgentResultProjectionBuilderOptions): AgentResultProjection {
  return {
    agent_run_id: assignment.agent_run_id,
    agent_id: assignment.agent_id,
    agent_kind: assignment.agent_kind,
    agent_display_name: "Pathfinder",
    outcome: "completed",
    summary: "Pathfinder found an exposed service.",
    final_checkpoint_id: "checkpoint-1",
    ...overrides,
    key_findings: [...(overrides.key_findings ?? ["HTTP exposed on 80"])],
    evidence_refs: (overrides.evidence_refs ?? [
      { kind: "artifact", path: "/workspace/artifacts/nmap.xml" },
    ]).map(reference => ({ ...reference })),
    tools_used: [...(overrides.tools_used ?? ["nmap"])],
    limitations: [...(overrides.limitations ?? [])],
    recommended_next_steps: [
      ...(overrides.recommended_next_steps ?? ["Review HTTP service headers"]),
    ],
  };
}
