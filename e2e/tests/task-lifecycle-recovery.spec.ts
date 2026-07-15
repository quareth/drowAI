/** Deterministic UI journey for task lifecycle, recovery, completion, and cleanup. */

import { expect, request, test, type APIRequestContext } from "@playwright/test";

import { createOwnerActor, installActorSession } from "../fixtures/actors";
import { sendChatMessageThroughUi } from "../fixtures/chat";
import {
  createEngagementThroughUi,
  createTaskThroughUiForEngagement,
  type TaskRecord,
} from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import {
  deleteTaskThroughUi,
  expectTaskStatusAfterRefresh,
  runTaskActionThroughUi,
} from "../fixtures/task-lifecycle";
import { openTaskInChat } from "../fixtures/tasks";

const JOURNEY_TIMEOUT_MS = 120_000;

test("persists deterministic task lifecycle and recovery", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "task-lifecycle" },
    });
    if (!stack.frontendUrl) {
      throw new Error("Task-lifecycle stack did not expose its frontend URL.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    await installActorSession(page, owner);
    await page.goto(stack.frontendUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByText("Operations").first()).toBeVisible({ timeout: 30_000 });

    const suffix = Date.now();
    const engagement = await createEngagementThroughUi(page, api, owner, {
      name: `Lifecycle engagement ${suffix}`,
    });
    const createdTasks: TaskRecord[] = [];

    const lifecycleTask = await createTaskThroughUiForEngagement(page, api, owner, engagement, {
      name: `Lifecycle task ${suffix}`,
      scope: "127.0.0.1",
    });
    createdTasks.push(lifecycleTask);
    await expectTaskStatusAfterRefresh(page, api, owner, engagement, lifecycleTask, "running");
    await runTaskActionThroughUi(page, api, owner, engagement, lifecycleTask, "Pause", "paused");
    await runTaskActionThroughUi(page, api, owner, engagement, lifecycleTask, "Resume", "running");
    await runTaskActionThroughUi(page, api, owner, engagement, lifecycleTask, "Stop", "stopped");
    await runTaskActionThroughUi(page, api, owner, engagement, lifecycleTask, "Start", "running");

    const cancellationTask = await createTaskThroughUiForEngagement(page, api, owner, engagement, {
      name: `Cancellation task ${suffix}`,
      scope: "127.0.0.2",
    });
    createdTasks.push(cancellationTask);
    await openTaskInChat(page, cancellationTask.id);
    const chatSubmission = page.waitForResponse((response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/tasks/${cancellationTask.id}/chat`),
    );
    await sendChatMessageThroughUi(page, "deterministic-cancellable-chat", {
      primaryMode: "agent_full",
      planMode: true,
    });
    const chatSubmissionResponse = await chatSubmission;
    expect(chatSubmissionResponse.ok()).toBe(true);
    const chatSubmissionPayload = (await chatSubmissionResponse.json()) as {
      queued?: boolean;
      turn_id?: string;
    };
    expect(chatSubmissionPayload.queued).not.toBe(true);
    expect(chatSubmissionPayload.turn_id).toMatch(/^task-\d+-turn-\d+$/);
    await page.reload({ waitUntil: "domcontentloaded" });
    await openTaskInChat(page, cancellationTask.id);
    const chatStop = page.getByTestId("chat-stop");
    await expect(chatStop).toBeVisible({ timeout: 30_000 });
    await chatStop.click();
    await expectPersistedRunState(api, owner.token, cancellationTask.id, "cancelled");
    await page.reload({ waitUntil: "domcontentloaded" });
    await openTaskInChat(page, cancellationTask.id);
    await expect(page.getByTestId("chat-send")).toBeVisible();
    await expectPersistedRunState(api, owner.token, cancellationTask.id, "cancelled");

    const recoveryTask = await createTaskThroughUiForEngagement(page, api, owner, engagement, {
      name: `Failure recovery task ${suffix}`,
      scope: "e2e://failure-retry",
    });
    createdTasks.push(recoveryTask);
    await expectTaskStatusAfterRefresh(page, api, owner, engagement, recoveryTask, "failed");
    await runTaskActionThroughUi(page, api, owner, engagement, recoveryTask, "Start", "running");

    const completionTask = await createTaskThroughUiForEngagement(page, api, owner, engagement, {
      name: `Completion task ${suffix}`,
      scope: "e2e://completion",
    });
    createdTasks.push(completionTask);
    await expectTaskStatusAfterRefresh(page, api, owner, engagement, completionTask, "completed");

    await runTaskActionThroughUi(page, api, owner, engagement, lifecycleTask, "Stop", "stopped");
    await runTaskActionThroughUi(page, api, owner, engagement, cancellationTask, "Stop", "stopped");
    await runTaskActionThroughUi(page, api, owner, engagement, recoveryTask, "Stop", "stopped");
    for (const task of createdTasks) {
      await deleteTaskThroughUi(page, api, owner, engagement, task);
    }
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

async function expectPersistedRunState(
  api: APIRequestContext,
  token: string,
  taskId: number,
  expectedState: string,
): Promise<void> {
  await expect
    .poll(async () => {
      const response = await api.get(`/api/interactive-runs/statuses?task_ids=${taskId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok()) return `http-${response.status()}`;
      const payload = (await response.json()) as {
        tasks?: Array<{ task_id?: number; run?: { state?: string } }>;
      };
      return payload.tasks?.find((item) => item.task_id === taskId)?.run?.state ?? "missing";
    }, { timeout: 30_000 })
    .toBe(expectedState);
}
