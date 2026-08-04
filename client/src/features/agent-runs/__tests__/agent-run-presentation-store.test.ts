// @vitest-environment jsdom
/**
 * Verifies task-scoped agent-run drawer state and its isolation from stream data.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  applyAgentRunLifecycleUpdate,
  getAgentRunSnapshot,
  resetAgentRunStoreForTests,
  useAgentRunStoreSnapshot,
} from "../state/agent-stream-store";
import {
  clearAgentRunPresentationForTask,
  closeAgentRunDrawer,
  getAgentRunPresentationSnapshot,
  MAX_AGENT_RUN_PRESENTATION_TASK_STATES,
  openAgentRunDetail,
  openAgentRunList,
  resetAgentRunPresentationStoreForTests,
  returnAgentRunDrawerToList,
  setAgentRunActivityExpanded,
  useAgentRunPresentationSnapshot,
} from "../state/agent-run-presentation-store";
import type { AgentRunLifecycleProjection } from "../contracts/agent-run";

const TASK_ID = 51201;
const OTHER_TASK_ID = 51202;

afterEach(() => {
  resetAgentRunStoreForTests();
  resetAgentRunPresentationStoreForTests();
});

function lifecycle(): AgentRunLifecycleProjection {
  return {
    agent_run_id: "run-1",
    agent_id: "pathfinder",
    agent_kind: "recon",
    agent_display_name: "Pathfinder",
    agent_icon_key: "pathfinder",
    status: "running",
    lifecycle_version: 1,
    task_id: TASK_ID,
    conversation_id: "conversation-1",
    parent_turn_id: "turn-1",
    parent_run_id: "parent-run-1",
    assignment: null,
    result: null,
    safe_error: null,
  };
}

describe("agent-run-presentation-store", () => {
  it("keeps stable closed snapshots isolated by task", () => {
    const initial = getAgentRunPresentationSnapshot(TASK_ID);

    expect(getAgentRunPresentationSnapshot(TASK_ID)).toBe(initial);
    expect(initial).toEqual({
      isOpen: false,
      parentRunId: null,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });

    openAgentRunList(TASK_ID, "parent-run-1");

    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: true,
      parentRunId: "parent-run-1",
    });
    expect(getAgentRunPresentationSnapshot(OTHER_TASK_ID)).toBe(initial);
  });

  it("navigates list and detail views while resetting expansion", () => {
    openAgentRunList(TASK_ID, "parent-run-1");
    openAgentRunDetail(TASK_ID, "parent-run-1", "run-1");
    setAgentRunActivityExpanded(TASK_ID, true);

    expect(getAgentRunPresentationSnapshot(TASK_ID)).toEqual({
      isOpen: true,
      parentRunId: "parent-run-1",
      view: "detail",
      selectedAgentRunId: "run-1",
      activityExpanded: true,
    });

    returnAgentRunDrawerToList(TASK_ID);
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toEqual({
      isOpen: true,
      parentRunId: "parent-run-1",
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });

    closeAgentRunDrawer(TASK_ID);
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toEqual({
      isOpen: false,
      parentRunId: null,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });
  });

  it("clears only the requested task and preserves other task snapshots", () => {
    const closedSnapshot = getAgentRunPresentationSnapshot(TASK_ID);
    openAgentRunList(TASK_ID, "parent-run-1");
    openAgentRunList(OTHER_TASK_ID, "parent-run-2");
    const otherSnapshot = getAgentRunPresentationSnapshot(OTHER_TASK_ID);

    clearAgentRunPresentationForTask(TASK_ID);

    expect(getAgentRunPresentationSnapshot(TASK_ID)).toBe(closedSnapshot);
    expect(getAgentRunPresentationSnapshot(OTHER_TASK_ID)).toBe(otherSnapshot);
  });

  it("bounds task persistence while retaining recently accessed snapshots", () => {
    for (
      let taskId = 1;
      taskId <= MAX_AGENT_RUN_PRESENTATION_TASK_STATES;
      taskId += 1
    ) {
      openAgentRunList(taskId, `parent-${taskId}`);
    }
    getAgentRunPresentationSnapshot(1);

    const addedTaskId = MAX_AGENT_RUN_PRESENTATION_TASK_STATES + 1;
    openAgentRunList(addedTaskId, `parent-${addedTaskId}`);

    expect(getAgentRunPresentationSnapshot(1).isOpen).toBe(true);
    expect(getAgentRunPresentationSnapshot(2).isOpen).toBe(false);
    expect(getAgentRunPresentationSnapshot(addedTaskId).isOpen).toBe(true);
  });

  it("does not rebuild stream snapshots for presentation changes", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 1);
    const dataSnapshot = getAgentRunSnapshot(TASK_ID);
    let renders = 0;
    renderHook(() => {
      renders += 1;
      return useAgentRunStoreSnapshot(TASK_ID);
    });

    act(() => openAgentRunDetail(TASK_ID, "parent-run-1", "run-1"));

    expect(getAgentRunSnapshot(TASK_ID)).toBe(dataSnapshot);
    expect(renders).toBe(1);
  });

  it("does not rebuild presentation snapshots for stream ingestion", () => {
    openAgentRunList(TASK_ID, "parent-run-1");
    const presentationSnapshot = getAgentRunPresentationSnapshot(TASK_ID);
    let renders = 0;
    renderHook(() => {
      renders += 1;
      return useAgentRunPresentationSnapshot(TASK_ID);
    });

    act(() => applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 1));

    expect(getAgentRunPresentationSnapshot(TASK_ID)).toBe(presentationSnapshot);
    expect(renders).toBe(1);
  });
});
