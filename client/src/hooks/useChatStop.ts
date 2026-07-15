/**
 * Task chat stop mutation for active interactive LangGraph runs.
 *
 * Responsibility: call the existing task-scoped chat cancellation endpoint
 * without coupling UI components to transport parsing or retry policy.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiRequest } from "@/lib/queryClient";

interface ChatStopParams {
  taskId: number;
  turnId?: string | null;
  reason?: "user_stop" | "explicit_cancel";
}

interface ChatStopToolCancellation {
  tool_call_ids?: string[];
  process_state?: string | null;
}

export interface ChatStopResult {
  task_id: number;
  turn_id?: string | null;
  cancelled: boolean;
  already_cancelled: boolean;
  active: boolean;
  status: string;
  reason?: string | null;
  cancel_reason?: string | null;
  tool_cancellation?: ChatStopToolCancellation | null;
}

function readErrorDetail(rawBody: string): string | null {
  if (!rawBody.trim()) {
    return null;
  }
  try {
    const parsed = JSON.parse(rawBody) as { detail?: unknown; message?: unknown };
    const detail = typeof parsed.detail === "string" ? parsed.detail : parsed.message;
    return typeof detail === "string" && detail.trim() ? detail.trim() : null;
  } catch {
    return rawBody.trim();
  }
}

export function useChatStop() {
  const queryClient = useQueryClient();

  return useMutation<ChatStopResult, Error, ChatStopParams>({
    mutationFn: async ({ taskId, turnId, reason = "user_stop" }: ChatStopParams) => {
      const payload: Record<string, unknown> = { reason };
      const canonicalTurnId = typeof turnId === "string" ? turnId.trim() : "";
      if (canonicalTurnId) {
        payload.turn_id = canonicalTurnId;
      }
      const res = await apiRequest("POST", `/api/tasks/${taskId}/chat/cancel`, payload);
      if (!res.ok) {
        const rawBody = await res.text();
        throw new Error(readErrorDetail(rawBody) || `Failed to stop chat (${res.status})`);
      }
      return res.json();
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["task-run-state-batch"] });
    },
  });
}

export default useChatStop;
