import { describe, expect, it } from "vitest";

import {
  isClarifyRequestPayload,
  isClarifyRequestInterruptDetail,
  isPlanReviewPayload,
  isPlanReviewInterruptDetail,
  isToolApprovalInterruptDetail,
  isToolApprovalPayload,
  type GraphInterruptEventDetail,
  type ClarifyRequestPayload,
  type PlanReviewPayload,
  type ToolApprovalPayload,
} from "@/types/hitl";

describe("hitl types", () => {
  it("identifies plan review payloads", () => {
    const payload: PlanReviewPayload = {
      type: "plan_review",
      goal: "Test",
      plan_steps: [],
      todo_list: [],
    };
    expect(isPlanReviewPayload(payload)).toBe(true);
    expect(isToolApprovalPayload(payload)).toBe(false);
  });

  it("identifies tool approval payloads", () => {
    const payload: ToolApprovalPayload = {
      type: "tool_approval",
      tool_id: "tool.test",
      tool_name: "Test Tool",
      parameters: {},
      description: "Test",
      items: [],
      tool_batch_id: "",
    };
    expect(isToolApprovalPayload(payload)).toBe(true);
    expect(isPlanReviewPayload(payload)).toBe(false);
  });

  it("identifies clarify request payloads", () => {
    const payload: ClarifyRequestPayload = {
      type: "clarify_request",
      questions: [
        {
          question_id: "target",
          input_type: "select",
          label: "What host should I scan?",
          options: ["10.0.0.1", "10.0.0.2"],
          required: true,
        },
      ],
      context_metadata: { source: "planner" },
    };
    expect(isClarifyRequestPayload(payload)).toBe(true);
    expect(isPlanReviewPayload(payload)).toBe(false);
    expect(isToolApprovalPayload(payload)).toBe(false);
  });

  it("narrows graph interrupt details to shared envelope subtypes", () => {
    const detail: GraphInterruptEventDetail = {
      taskId: 1,
      threadId: "task-1",
      interruptId: "intr-1",
      interruptType: "tool_approval",
      graphName: "simple_tool",
      payload: {
        type: "tool_approval",
        tool_id: "tool.test",
        tool_name: "Test Tool",
        parameters: {},
        description: "Test",
        items: [],
        tool_batch_id: "",
      },
    };

    expect(isToolApprovalInterruptDetail(detail)).toBe(true);
    expect(isPlanReviewInterruptDetail(detail)).toBe(false);
    expect(isClarifyRequestInterruptDetail(detail)).toBe(false);
  });
});
