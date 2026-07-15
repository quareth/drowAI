/**
 * HITL (Human-in-the-Loop) interrupt payloads and streaming events.
 *
 * Hand-maintained mirror of `backend/services/langgraph_chat/checkpoint/hitl_schemas.py`
 * (`ToolApprovalPayload`, `ToolApprovalItem`, `PlanReviewPayload`,
 * `ClarifyRequestPayload`). The codegen at
 * `backend/scripts/generate_streaming_types.py` only covers stream-event
 * types from `stream_event_schema.py`, so this file is intentionally
 * hand-maintained. When you change the Pydantic schema, mirror the change
 * here in the same PR.
 *
 * Optionality convention: a field is `required` here when the Pydantic
 * model guarantees its presence on the wire (either via `Field(...)` or
 * via a non-`None` default). Fields whose Pydantic default is `None` are
 * marked optional with `?`. The single legacy carve-out is
 * `ToolApprovalItem.tool_call_id`, which the frontend's deriveItems
 * fallback (legacy single-tool payloads) sets to `undefined`.
 */

export interface ToolApprovalItem {
  tool_call_id?: string;
  tool_id: string;
  tool_name: string;
  parameters: Record<string, unknown>;
  description: string;
  risk_level?: "low" | "medium" | "high";
}

export interface ToolApprovalPayload {
  type: "tool_approval";
  interrupt_id?: string;
  // Legacy single-tool fields populated from items[0] during the migration
  // window so frontends that haven't picked up the batch shape still render
  // the first call.
  tool_id: string;
  tool_name: string;
  parameters: Record<string, unknown>;
  description: string;
  risk_level?: "low" | "medium" | "high";
  estimated_duration?: number;
  // Phase 7 Task 7.1: batch-aware fields. ``items`` carries every committed
  // call; ``tool_batch_id`` ties the approval surface to the batch.
  items: ToolApprovalItem[];
  tool_batch_id: string;
  turn_sequence?: number;
  turn_id?: string;
  reserved_message_id?: number;
}

export interface TodoItem {
  id: string;
  text: string;
  status: "pending" | "in_progress" | "completed" | "skipped";
  completedAt?: string;
}

export interface PlanReviewPayload {
  type: "plan_review";
  interrupt_id?: string;
  goal: string;
  plan_steps: string[];
  todo_list: TodoItem[];
  reasoning?: string;
  targets?: string[];
  run_id?: number;
  plan_version?: number;
  turn_sequence?: number;
  turn_id?: string;
  reserved_message_id?: number;
}

export interface ClarifyQuestionPayload {
  question_id: string;
  input_type: "select";
  label: string;
  options: string[];
  required?: boolean;
}

export interface ClarifyRequestPayload {
  type: "clarify_request";
  interrupt_id?: string;
  questions: ClarifyQuestionPayload[];
  context_metadata?: Record<string, unknown>;
}

export type InterruptPayload = ToolApprovalPayload | PlanReviewPayload | ClarifyRequestPayload;
export type InterruptType = InterruptPayload["type"];

export function isPlanReviewPayload(
  payload: InterruptPayload,
): payload is PlanReviewPayload {
  return payload.type === "plan_review";
}

export function isToolApprovalPayload(
  payload: InterruptPayload,
): payload is ToolApprovalPayload {
  return payload.type === "tool_approval";
}

export function isClarifyRequestPayload(
  payload: InterruptPayload,
): payload is ClarifyRequestPayload {
  return payload.type === "clarify_request";
}

export interface GraphInterruptEvent {
  type: "graph_interrupt";
  task_id: number;
  thread_id: string;
  interrupt_id: string;
  checkpoint_id?: string | null;
  interrupt_type: "tool_approval" | "plan_review" | "clarify_request";
  graph_name: string;
  payload: InterruptPayload;
  timestamp: string;
}

export interface GraphInterruptEventDetail {
  taskId: number;
  threadId: string;
  interruptId: string;
  checkpointId?: string | null;
  interruptType: InterruptType;
  graphName: string;
  payload: InterruptPayload;
}

export interface InterruptEnvelopeDetail<TType extends InterruptType, TPayload extends InterruptPayload>
  extends Omit<GraphInterruptEventDetail, "interruptType" | "payload"> {
  interruptType: TType;
  payload: TPayload;
}

export type ToolApprovalInterruptDetail = InterruptEnvelopeDetail<"tool_approval", ToolApprovalPayload>;
export type PlanReviewInterruptDetail = InterruptEnvelopeDetail<"plan_review", PlanReviewPayload>;
export type ClarifyRequestInterruptDetail = InterruptEnvelopeDetail<"clarify_request", ClarifyRequestPayload>;

export function isToolApprovalInterruptDetail(
  detail: GraphInterruptEventDetail | null | undefined,
): detail is ToolApprovalInterruptDetail {
  return detail != null && detail.interruptType === "tool_approval" && isToolApprovalPayload(detail.payload);
}

export function isPlanReviewInterruptDetail(
  detail: GraphInterruptEventDetail | null | undefined,
): detail is PlanReviewInterruptDetail {
  return detail != null && detail.interruptType === "plan_review" && isPlanReviewPayload(detail.payload);
}

export function isClarifyRequestInterruptDetail(
  detail: GraphInterruptEventDetail | null | undefined,
): detail is ClarifyRequestInterruptDetail {
  return (
    detail != null &&
    detail.interruptType === "clarify_request" &&
    isClarifyRequestPayload(detail.payload)
  );
}
