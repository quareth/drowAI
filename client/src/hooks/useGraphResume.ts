import { useMutation } from "@tanstack/react-query";

import { apiRequest } from "@/lib/queryClient";

interface ResumeParams {
  taskId: number;
  interruptType: "tool_approval" | "plan_review" | "clarify_request";
  /** Canonical interrupt ID from interrupt snapshot/SSE. Required (Task 6.2). */
  interruptId: string;
  graphName?: string;
  response: {
    action: "approve" | "edit" | "skip" | "reject" | "answer";
    edited_parameters?: Record<string, unknown>;
    edited_goal?: string;
    edited_plan_steps?: string[];
    edited_todo_list?: string[];
    answers?: Record<string, string>;
    tool_batch_id?: string;
    decisions?: Array<{
      tool_call_id?: string;
      action: "approve" | "edit" | "skip";
      edited_parameters?: Record<string, unknown>;
    }>;
    user_note?: string;
  };
}

export function useGraphResume() {
  return useMutation({
    mutationFn: async ({ taskId, interruptType, interruptId, graphName, response }: ResumeParams) => {
      const canonicalInterruptId = typeof interruptId === "string" ? interruptId.trim() : "";
      if (!canonicalInterruptId) {
        throw new Error("interrupt_id is required");
      }
      const payload: Record<string, unknown> = {
        interrupt_type: interruptType,
        response,
      };
      payload.interrupt_id = canonicalInterruptId;
      if (graphName) {
        payload.graph_name = graphName;
      }
      const res = await apiRequest("POST", `/api/tasks/${taskId}/graph/resume`, payload);
      if (!res.ok) {
        const rawBody = await res.text();
        let detail: string | null = null;
        if (rawBody) {
          try {
            const parsed = JSON.parse(rawBody) as { detail?: unknown };
            if (typeof parsed.detail === "string" && parsed.detail.trim()) {
              detail = parsed.detail.trim();
            }
          } catch {
            detail = rawBody.trim() || null;
          }
        }
        throw new Error(detail || `Failed to resume graph (${res.status})`);
      }
      return res.json();
    },
  });
}
