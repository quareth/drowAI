/**
 * Task helpers for deterministic Playwright smoke tests.
 *
 * This module owns task API setup, UI creation, and task selector access so
 * smoke specs share one task workflow contract.
 */

import { expect, type APIRequestContext, type Page } from "@playwright/test";

export interface TaskRecord {
  id: number;
  name: string;
  status?: string;
}

export async function createTask(
  api: APIRequestContext,
  token: string,
  name: string,
): Promise<TaskRecord> {
  const response = await api.post("/api/tasks/", {
    headers: authHeaders(token),
    data: {
      name,
      description: "Deterministic core workflow smoke task",
      scope: "127.0.0.1",
    },
  });
  if (!response.ok()) {
    throw new Error(`Task create failed: ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as TaskRecord;
}

export async function createTaskThroughUi(
  page: Page,
  api: APIRequestContext,
  token: string,
  name: string,
): Promise<TaskRecord> {
  await page.getByRole("button", { name: "New", exact: true }).click();
  await page.getByRole("menuitem", { name: "New Task" }).click();
  await expect(page.getByRole("dialog", { name: "Create New Task" })).toBeVisible();
  await page.getByLabel("Task Name").fill(name);
  await page.getByLabel("Target Scope").fill("127.0.0.1");
  await page.getByRole("button", { name: "Create Task" }).click();

  await expect(page.getByRole("dialog", { name: "Create New Task" })).toBeHidden({
    timeout: 30_000,
  });
  const task = await waitForTaskByName(api, token, name);
  await expect(page.getByText(name).first()).toBeVisible({ timeout: 30_000 });
  return task;
}

export async function openTaskInChat(page: Page, taskId: number): Promise<void> {
  const taskSelector = page.getByLabel("Select task");
  await expect(taskSelector).toBeVisible({ timeout: 30_000 });
  const selectedTaskPattern = new RegExp(`\\(#${taskId}\\)`);

  const tagName = await taskSelector.evaluate((element) => element.tagName.toLowerCase());
  if (tagName === "select") {
    await taskSelector.selectOption(String(taskId));
    await expect(taskSelector).toHaveValue(String(taskId));
    return;
  }

  await taskSelector.click();
  const option = page.getByRole("option", { name: new RegExp(`\\(#${taskId}\\)`) }).first();
  await expect(option).toBeVisible({ timeout: 30_000 });
  await option.click();
  await expect(taskSelector).toContainText(selectedTaskPattern, { timeout: 30_000 });
}

export async function listTasks(
  api: APIRequestContext,
  token: string,
): Promise<TaskRecord[]> {
  const response = await api.get("/api/tasks/", {
    headers: authHeaders(token),
  });
  if (!response.ok()) {
    throw new Error(`Task list failed: ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as TaskRecord[];
}

export async function getTaskResponse(
  api: APIRequestContext,
  token: string,
  taskId: number,
) {
  return api.get(`/api/tasks/${taskId}`, {
    headers: authHeaders(token),
  });
}

async function waitForTaskByName(
  api: APIRequestContext,
  token: string,
  name: string,
): Promise<TaskRecord> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const tasks = await listTasks(api, token);
    const task = tasks.find((candidate) => candidate.name === name);
    if (task) {
      return task;
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`Task did not appear in API list: ${name}`);
}

function authHeaders(token: string): { Authorization: string } {
  return { Authorization: `Bearer ${token}` };
}
