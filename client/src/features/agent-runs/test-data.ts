/** Fresh linked contract builders shared by frontend agent-run tests. */

import type {
  AgentAssignment,
  AgentResultProjection,
} from "./contracts/agent-run";

export function buildAgentAssignment(
  overrides: Partial<AgentAssignment> = {},
): AgentAssignment {
  return {
    assignment_id: "assign-1",
    agent_run_id: "run-1",
    agent_id: "pathfinder",
    agent_kind: "recon",
    task_id: 42,
    conversation_id: "conversation-1",
    parent_turn_id: "turn-1",
    objective: "Map open services on the approved target.",
    scope_summary: "Approved internal test host only.",
    ...overrides,
    targets: [...(overrides.targets ?? ["10.0.0.10"])],
    suggested_capabilities: [
      ...(overrides.suggested_capabilities ?? ["host_discovery", "port_scan"]),
    ],
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
