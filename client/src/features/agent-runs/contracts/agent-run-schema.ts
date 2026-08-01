/**
 * Runtime schemas for backend-owned subagent-run projections.
 *
 * This module is the frontend authority for validating assignment, runtime,
 * result, lifecycle, and process-local status payloads before store ingestion.
 */
import { z } from "zod";

const nonEmptyString = z.string().trim().min(1);
const optionalNullableString = nonEmptyString.nullable().optional();
const positiveInteger = z.number().int().positive();
const agentKindSchema = nonEmptyString.regex(/^[a-z][a-z0-9_]*$/);
const stringListSchema = z.array(nonEmptyString);

export type AgentJsonValue =
  | string
  | number
  | boolean
  | null
  | AgentJsonValue[]
  | { [key: string]: AgentJsonValue };

const agentJsonValueSchema: z.ZodType<AgentJsonValue> = z.lazy(() =>
  z.union([
    z.string(),
    z.number().finite(),
    z.boolean(),
    z.null(),
    z.array(agentJsonValueSchema),
    z.record(agentJsonValueSchema),
  ]),
);

export const agentCredentialReferenceSchema = z
  .object({
    provider: nonEmptyString,
    credential_id: nonEmptyString,
  })
  .strict();

export const agentRuntimeIdentitySchema = z
  .object({
    tenant_id: positiveInteger,
    task_id: positiveInteger,
    user_id: positiveInteger.nullable().optional(),
    workspace_id: nonEmptyString,
    workspace_path: optionalNullableString,
    runtime_placement_mode: nonEmptyString,
    actor_type: nonEmptyString,
    actor_id: nonEmptyString,
    runner_id: optionalNullableString,
    execution_site_id: optionalNullableString,
    provider: optionalNullableString,
    model: optionalNullableString,
    reasoning_effort: optionalNullableString,
    feature_flags: z.record(z.boolean()),
    credential_ref: agentCredentialReferenceSchema.nullable().optional(),
  })
  .strict();

export const agentAssignmentSchema = z
  .object({
    assignment_id: nonEmptyString,
    agent_run_id: nonEmptyString,
    agent_id: nonEmptyString,
    agent_kind: agentKindSchema,
    task_id: positiveInteger,
    tenant_id: positiveInteger,
    conversation_id: nonEmptyString,
    parent_turn_id: nonEmptyString,
    parent_graph_thread_id: nonEmptyString,
    objective: nonEmptyString,
    targets: stringListSchema,
    suggested_capabilities: stringListSchema,
    scope_summary: optionalNullableString,
    relevant_context: z.record(agentJsonValueSchema),
    runtime_identity: agentRuntimeIdentitySchema,
  })
  .strict()
  .superRefine((assignment, context) => {
    if (assignment.runtime_identity.tenant_id !== assignment.tenant_id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["runtime_identity", "tenant_id"],
        message: "runtime tenant must match assignment tenant",
      });
    }
    if (assignment.runtime_identity.task_id !== assignment.task_id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["runtime_identity", "task_id"],
        message: "runtime task must match assignment task",
      });
    }
  });

export const agentEvidenceRefSchema = z.record(nonEmptyString);

export const agentResultProjectionSchema = z
  .object({
    agent_run_id: nonEmptyString,
    agent_id: nonEmptyString,
    agent_kind: agentKindSchema,
    agent_display_name: nonEmptyString,
    outcome: z.enum(["completed", "partial", "blocked", "failed", "cancelled"]),
    summary: nonEmptyString,
    key_findings: stringListSchema,
    evidence_refs: z.array(agentEvidenceRefSchema),
    tools_used: stringListSchema,
    limitations: stringListSchema,
    recommended_next_steps: stringListSchema,
    final_checkpoint_id: optionalNullableString,
  })
  .strict();

const agentRunLifecycleFields = {
  agent_run_id: nonEmptyString,
  agent_id: nonEmptyString,
  agent_kind: agentKindSchema,
  agent_display_name: nonEmptyString,
  agent_icon_key: optionalNullableString,
  status: z.enum([
    "queued",
    "running",
    "waiting_for_approval",
    "completed",
    "failed",
    "cancelled",
  ]),
  lifecycle_version: positiveInteger,
  task_id: positiveInteger,
  conversation_id: nonEmptyString,
  parent_turn_id: nonEmptyString,
  parent_run_id: optionalNullableString,
  assignment: agentAssignmentSchema.nullable().optional(),
  result: agentResultProjectionSchema.nullable().optional(),
  safe_error: optionalNullableString,
};

export const agentRunLifecycleProjectionSchema = z
  .object(agentRunLifecycleFields)
  .strict()
  .superRefine(validateLifecycleIdentity);

export const localAgentRunStatusProjectionSchema = z
  .object({
    ...agentRunLifecycleFields,
    assignment: agentAssignmentSchema,
    cancel_requested: z.boolean(),
    created_at: nonEmptyString,
    started_at: optionalNullableString,
    completed_at: optionalNullableString,
  })
  .strict()
  .superRefine(validateLifecycleIdentity);

export const localAgentRunListEnvelopeSchema = z
  .object({
    process_local: z.literal(true),
    task_id: positiveInteger,
    agent_runs: z.array(z.unknown()),
  })
  .strict();

function validateLifecycleIdentity(
  lifecycle: {
    agent_run_id: string;
    agent_id: string;
    agent_kind: string;
    task_id: number;
    conversation_id: string;
    parent_turn_id: string;
    assignment?: z.infer<typeof agentAssignmentSchema> | null;
    result?: z.infer<typeof agentResultProjectionSchema> | null;
  },
  context: z.RefinementCtx,
): void {
  const assignment = lifecycle.assignment;
  if (assignment) {
    const assignmentFields = [
      ["agent_run_id", lifecycle.agent_run_id],
      ["agent_id", lifecycle.agent_id],
      ["agent_kind", lifecycle.agent_kind],
      ["task_id", lifecycle.task_id],
      ["conversation_id", lifecycle.conversation_id],
      ["parent_turn_id", lifecycle.parent_turn_id],
    ] as const;
    for (const [field, expected] of assignmentFields) {
      if (assignment[field] !== expected) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["assignment", field],
          message: `assignment ${field} must match lifecycle projection`,
        });
      }
    }
  }

  const result = lifecycle.result;
  if (result) {
    const resultFields = [
      ["agent_run_id", lifecycle.agent_run_id],
      ["agent_id", lifecycle.agent_id],
      ["agent_kind", lifecycle.agent_kind],
    ] as const;
    for (const [field, expected] of resultFields) {
      if (result[field] !== expected) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["result", field],
          message: `result ${field} must match lifecycle projection`,
        });
      }
    }
  }
}

export type AgentCredentialReference = z.infer<typeof agentCredentialReferenceSchema>;
export type AgentRuntimeIdentity = z.infer<typeof agentRuntimeIdentitySchema>;
export type AgentAssignment = z.infer<typeof agentAssignmentSchema>;
export type AgentEvidenceRef = z.infer<typeof agentEvidenceRefSchema>;
export type AgentResultProjection = z.infer<typeof agentResultProjectionSchema>;
export type AgentRunLifecycleProjection = z.infer<
  typeof agentRunLifecycleProjectionSchema
>;
export type LocalAgentRunStatusProjection = z.infer<
  typeof localAgentRunStatusProjectionSchema
>;
