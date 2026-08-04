// @vitest-environment jsdom
/**
 * Verifies successful task deletion clears task-scoped subagent-run state.
 */
import type { PropsWithChildren } from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyAgentRunActivityPayload,
  getAgentRunSnapshot,
  resetAgentRunStoreForTests,
} from "@/features/agent-runs/state/agent-stream-store";
import {
  getAgentRunPresentationSnapshot,
  openAgentRunDetail,
  resetAgentRunPresentationStoreForTests,
} from "@/features/agent-runs/state/agent-run-presentation-store";
import { useTaskPanelMutations } from "@/hooks/use-task-panel";
import { queryClient } from "@/lib/queryClient";
import type { Task } from "@/types";

const mocked = vi.hoisted(() => ({
  apiRequest: vi.fn(),
}));

vi.mock("@/lib/queryClient", async importOriginal => {
  const actual = await importOriginal<typeof import("@/lib/queryClient")>();
  return { ...actual, apiRequest: mocked.apiRequest };
});

vi.mock("@/hooks/use-engagement-knowledge", () => ({
  invalidateEngagementKnowledgeQueries: vi.fn(),
  useEngagements: () => ({ data: { items: [] } }),
}));

const TASK_ID = 84101;

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

afterEach(() => {
  mocked.apiRequest.mockReset();
  queryClient.clear();
  resetAgentRunStoreForTests();
  resetAgentRunPresentationStoreForTests();
});

describe("useTaskPanelMutations", () => {
  it("clears agent-run records and drawer selection after successful deletion", async () => {
    applyAgentRunActivityPayload(TASK_ID, {
      type: "tool_start",
      content: "starting",
      task_id: TASK_ID,
      sequence: 1,
      metadata: {
        producer_type: "subagent",
        agent_run_id: "run-1",
        agent_id: "pathfinder",
        agent_kind: "recon",
        parent_turn_id: "turn-1",
        parent_run_id: "parent-run-1",
        sequence: 1,
      },
    });
    openAgentRunDetail(TASK_ID, "parent-run-1", "run-1");
    expect(getAgentRunPresentationSnapshot(TASK_ID).selectedAgentRunId).toBe("run-1");

    mocked.apiRequest.mockResolvedValue(new Response(null, { status: 204 }));
    const task: Task = {
      id: TASK_ID,
      user_id: 1,
      engagement_id: null,
      name: "Task",
      status: "created",
      created_at: "2026-08-01T09:00:00Z",
      updated_at: "2026-08-01T09:00:00Z",
    };
    const clearPlanState = vi.fn();
    const { result } = renderHook(
      () => useTaskPanelMutations({ tasks: [task], clearPlanState }),
      { wrapper },
    );

    await act(async () => {
      await result.current.deleteTaskMutation.mutateAsync(TASK_ID);
    });

    expect(mocked.apiRequest).toHaveBeenCalledWith(
      "DELETE",
      `/api/tasks/${TASK_ID}`,
    );
    expect(clearPlanState).toHaveBeenCalledWith(TASK_ID);
    expect(getAgentRunSnapshot(TASK_ID).runs).toEqual([]);
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: false,
      selectedAgentRunId: null,
    });
  });
});
