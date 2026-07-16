/** UI and authenticated-API assertions for deterministic task lifecycle journeys. */

import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

import { actorHeaders, type E2EActor } from "./actors";
import type { EngagementRecord, TaskRecord } from "./domain-fixtures";

const STATUS_POLL_ATTEMPTS = 40;
const STATUS_POLL_DELAY_MS = 250;

export interface TaskStatusPollOptions {
  maxDelays?: number;
  delayMs?: number;
}

export function taskCard(page: Page, taskId: number): Locator {
  return page.getByTestId(`task-card-${taskId}`);
}

export async function runTaskActionThroughUi(
  page: Page,
  api: APIRequestContext,
  actor: E2EActor,
  engagement: EngagementRecord,
  task: TaskRecord,
  action: "Start" | "Pause" | "Resume" | "Stop",
  expectedStatus: string,
): Promise<void> {
  const card = await ensureTaskCardVisible(page, engagement, task.id);
  await card.getByRole("button", { name: action, exact: true }).click();
  await waitForTaskStatus(api, actor, task.id, expectedStatus);
  await expectTaskStatusAfterRefresh(page, api, actor, engagement, task, expectedStatus);
}

export async function expectTaskStatusAfterRefresh(
  page: Page,
  api: APIRequestContext,
  actor: E2EActor,
  engagement: EngagementRecord,
  task: TaskRecord,
  expectedStatus: string,
): Promise<void> {
  await waitForTaskStatus(api, actor, task.id, expectedStatus);
  await expectTaskCardStatus(await ensureTaskCardVisible(page, engagement, task.id), expectedStatus);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expectTaskCardStatus(await ensureTaskCardVisible(page, engagement, task.id), expectedStatus);
  const persisted = await fetchTask(api, actor, task.id);
  expect(persisted.status).toBe(expectedStatus);
}

export async function deleteTaskThroughUi(
  page: Page,
  api: APIRequestContext,
  actor: E2EActor,
  engagement: EngagementRecord,
  task: TaskRecord,
): Promise<void> {
  const card = await ensureTaskCardVisible(page, engagement, task.id);
  page.once("dialog", (dialog) => dialog.accept());
  await card.getByRole("button", { name: `Task actions for ${task.name}` }).click();
  await page.getByRole("menuitem", { name: "Delete Task" }).click();
  await expect(taskCard(page, task.id)).toBeHidden({ timeout: 30_000 });
  await expectTaskMissing(api, actor, task.id);
}

async function expectTaskCardStatus(card: Locator, expectedStatus: string): Promise<void> {
  const label = expectedStatus.charAt(0).toUpperCase() + expectedStatus.slice(1);
  await expect(card.locator(".status-indicator")).toHaveText(label, { timeout: 30_000 });
}

async function ensureTaskCardVisible(
  page: Page,
  engagement: EngagementRecord,
  taskId: number,
): Promise<Locator> {
  const card = taskCard(page, taskId);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (await card.isVisible()) {
      return card;
    }
    const engagementToggle = page.getByRole("button", { name: engagement.name, exact: true });
    await expect(engagementToggle).toBeVisible({ timeout: 30_000 });
    if ((await engagementToggle.getAttribute("aria-expanded")) !== "true") {
      await engagementToggle.click();
    }
    try {
      await card.waitFor({ state: "visible", timeout: 2_000 });
      return card;
    } catch {
      // Query hydration can race the engagement's automatic expansion; retry its current state.
    }
  }
  await expect(card).toBeVisible({ timeout: 30_000 });
  return card;
}

export async function waitForTaskStatus(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
  expectedStatus: string,
  options: TaskStatusPollOptions = {},
): Promise<TaskRecord> {
  const maxDelays = options.maxDelays ?? STATUS_POLL_ATTEMPTS;
  const delayMs = options.delayMs ?? STATUS_POLL_DELAY_MS;
  let task = await fetchTask(api, actor, taskId);

  for (let delayCount = 0; task.status !== expectedStatus && delayCount < maxDelays; delayCount += 1) {
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    task = await fetchTask(api, actor, taskId);
  }

  if (task.status === expectedStatus) {
    return task;
  }
  throw new Error(`Task ${taskId} remained ${task.status}; expected ${expectedStatus}.`);
}

async function fetchTask(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
): Promise<TaskRecord> {
  const response = await api.get(`/api/tasks/${taskId}`, { headers: actorHeaders(actor) });
  if (!response.ok()) {
    throw new Error(`Task read failed: ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as TaskRecord;
}

async function expectTaskMissing(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
): Promise<void> {
  for (let attempt = 0; attempt < STATUS_POLL_ATTEMPTS; attempt += 1) {
    const response = await api.get(`/api/tasks/${taskId}`, { headers: actorHeaders(actor) });
    if (response.status() === 404) {
      return;
    }
    if (!response.ok()) {
      throw new Error(`Task delete readback failed: ${response.status()} ${await response.text()}`);
    }
    await new Promise((resolve) => setTimeout(resolve, STATUS_POLL_DELAY_MS));
  }
  throw new Error(`Deleted task ${taskId} remained accessible through the authenticated API.`);
}
