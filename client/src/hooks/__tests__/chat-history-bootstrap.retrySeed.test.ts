// @vitest-environment jsdom
/**
 * Phase 6 Task 6.3 tests for ``seedRetryStateFromTranscriptItems``.
 *
 * Bootstrap MUST trust the server-authoritative retry projection (Phase
 * 4) and seed the retry-state store from transcript metadata, so that
 * after a remount the retry CTA reflects the canonical lifecycle state
 * rather than re-deriving retryability from local cues.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  seedRetryStateFromTranscriptItems,
  type ChatTranscriptItem,
} from "@/hooks/chat-history-bootstrap";
import {
  __resetRetryStateStoreForTest,
  applyRetryStateUpdate,
  getRetryStateForTurn,
} from "@/state/retry-state-store";

const TASK_ID = 99001;

afterEach(() => {
  __resetRetryStateStoreForTest();
});

function makeItem(metadata: Record<string, unknown>): ChatTranscriptItem {
  return {
    id: "msg-1",
    kind: "assistant",
    turn_number: 3,
    content: "",
    metadata,
  };
}

describe("seedRetryStateFromTranscriptItems", () => {
  it("ignores items without retry_state metadata", () => {
    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({ status: "completed" }),
      makeItem({ turn_id: "task-99001-turn-3" }),
    ]);

    expect(getRetryStateForTurn(TASK_ID, "task-99001-turn-3")).toBeNull();
  });

  it("seeds an in-flight retrying entry from RETRYING projection metadata", () => {
    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "retrying",
        active_retry: true,
        retry_attempt: 1,
        retry_max_attempts: 2,
        workflow_id: 14,
      }),
    ]);

    const entry = getRetryStateForTurn(TASK_ID, "task-99001-turn-3");
    expect(entry).toMatchObject({
      taskId: TASK_ID,
      turnId: "task-99001-turn-3",
      workflowId: 14,
      state: "retrying",
      retryAttempt: 1,
      retryMaxAttempts: 2,
      inFlight: true,
    });
  });

  it("seeds a completed terminal entry from COMPLETED projection metadata", () => {
    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "completed",
        retry_attempt: 1,
        retry_max_attempts: 2,
      }),
    ]);

    const entry = getRetryStateForTurn(TASK_ID, "task-99001-turn-3");
    expect(entry).toMatchObject({
      state: "completed",
      inFlight: false,
    });
  });

  it("does not revive a completed entry when an older failed projection is present", () => {
    // First seed completed (e.g. canonical state for the resolved retry).
    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "completed",
      }),
    ]);

    // Then a stale "failed" projection arrives — must NOT override the
    // sticky-terminal completed state in the store. This guards against
    // bootstrap pages racing the projection refresh and reopening a
    // stale retry CTA.
    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "failed",
        retryable: true,
      }),
    ]);

    const entry = getRetryStateForTurn(TASK_ID, "task-99001-turn-3");
    expect(entry?.state).toBe("completed");
    expect(entry?.inFlight).toBe(false);
  });

  it("respects the store's sticky-completed invariant when bootstrap returns later", () => {
    // Stream-event ordering: completion arrives first via stream events
    // (e.g. while the user was on another tab), then bootstrap returns
    // the canonical projection. The bootstrap-seeded ``completed`` state
    // must stay completed even though a re-render of the same metadata
    // arrives.
    applyRetryStateUpdate({
      taskId: TASK_ID,
      turnId: "task-99001-turn-3",
      workflowId: 14,
      state: "completed",
    });

    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({
        turn_id: "task-99001-turn-3",
        workflow_id: 14,
        retry_state: "completed",
      }),
    ]);

    expect(getRetryStateForTurn(TASK_ID, "task-99001-turn-3")?.state).toBe("completed");
  });

  it("ignores items that name an unknown lifecycle state", () => {
    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "totally_made_up",
      }),
    ]);

    expect(getRetryStateForTurn(TASK_ID, "task-99001-turn-3")).toBeNull();
  });

  it("ignores items missing turn_id", () => {
    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({
        retry_state: "retrying",
      }),
    ]);

    // No way to address the entry without a turn_id; nothing should be
    // written.
    expect(getRetryStateForTurn(TASK_ID, "")).toBeNull();
  });

  it("rejects invalid task ids", () => {
    seedRetryStateFromTranscriptItems(0, [
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "retrying",
      }),
    ]);
    seedRetryStateFromTranscriptItems(-1, [
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "retrying",
      }),
    ]);

    expect(getRetryStateForTurn(TASK_ID, "task-99001-turn-3")).toBeNull();
  });

  it("seeds at most one entry per turn_id when multiple items reference it", () => {
    seedRetryStateFromTranscriptItems(TASK_ID, [
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "retrying",
        retry_attempt: 1,
        retry_max_attempts: 2,
      }),
      // A second metadata blob (e.g. a tool detail row also carrying
      // the projected retry metadata) for the SAME turn must not
      // overwrite the assistant's canonical entry from the same
      // bootstrap pass.
      makeItem({
        turn_id: "task-99001-turn-3",
        retry_state: "completed",
        retry_attempt: 1,
        retry_max_attempts: 2,
      }),
    ]);

    const entry = getRetryStateForTurn(TASK_ID, "task-99001-turn-3");
    expect(entry?.state).toBe("retrying");
  });
});
