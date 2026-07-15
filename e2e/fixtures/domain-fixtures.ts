/** Authenticated engagement/task builders with persisted-state assertions. */

import { expect, type APIRequestContext, type Page } from "@playwright/test";

import { actorHeaders, type E2EActor } from "./actors";

export interface EngagementRecord {
  id: number;
  user_id: number;
  name: string;
  description?: string | null;
  status: string;
}

export interface TaskRecord {
  id: number;
  user_id: number;
  engagement_id: number;
  name: string;
  description?: string | null;
  scope?: string | null;
  status: string;
}

export interface EngagementInput {
  name: string;
  description?: string;
}

export interface TaskInput {
  name: string;
  description?: string;
  scope?: string;
}

export async function createEngagement(
  api: APIRequestContext,
  actor: E2EActor,
  input: EngagementInput,
): Promise<EngagementRecord> {
  const response = await api.post("/api/engagements/", {
    headers: actorHeaders(actor),
    data: input,
  });
  return readSuccessfulJson<EngagementRecord>(response, "Engagement create");
}

export async function createEngagementThroughUi(
  page: Page,
  api: APIRequestContext,
  actor: E2EActor,
  input: EngagementInput,
): Promise<EngagementRecord> {
  await openCreateMenuItem(page, "New Engagement");
  const dialog = page.getByRole("dialog", { name: "New engagement" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Name").fill(input.name);
  if (input.description) {
    await dialog.getByLabel("Description (optional)").fill(input.description);
  }
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden({ timeout: 30_000 });
  const engagement = await waitForEngagementByName(api, actor, input.name);
  await expect(page.getByText(input.name, { exact: true }).first()).toBeVisible();
  return engagement;
}

export async function createTaskForEngagement(
  api: APIRequestContext,
  actor: E2EActor,
  engagement: EngagementRecord,
  input: TaskInput,
): Promise<TaskRecord> {
  const response = await api.post("/api/tasks/", {
    headers: actorHeaders(actor),
    data: { ...input, engagement_id: engagement.id },
  });
  return readSuccessfulJson<TaskRecord>(response, "Task create");
}

export async function createTaskThroughUiForEngagement(
  page: Page,
  api: APIRequestContext,
  actor: E2EActor,
  engagement: EngagementRecord,
  input: TaskInput,
): Promise<TaskRecord> {
  await openCreateMenuItem(page, "New Task");
  const dialog = page.getByRole("dialog", { name: "Create New Task" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Task Name").fill(input.name);
  if (input.scope) {
    await dialog.getByLabel("Target Scope").fill(input.scope);
  }
  await dialog.getByRole("combobox").click();
  const engagementOption = page.getByRole("option", {
    name: new RegExp(escapeRegex(engagement.name)),
  });
  await expect(engagementOption).toBeVisible();
  await engagementOption.click();
  await dialog.getByRole("button", { name: "Create Task" }).click();
  await expect(dialog).toBeHidden({ timeout: 30_000 });
  const task = await waitForTaskByName(api, actor, input.name);
  await expect(page.getByText(input.name, { exact: true }).first()).toBeVisible();
  return task;
}

export async function assertPersistedEngagement(
  api: APIRequestContext,
  actor: E2EActor,
  expected: EngagementRecord,
): Promise<void> {
  const response = await api.get(`/api/engagements/${expected.id}`, {
    headers: actorHeaders(actor),
  });
  const actual = await readSuccessfulJson<EngagementRecord>(response, "Engagement readback");
  assertFieldsEqual(actual, expected, ["id", "user_id", "name", "description", "status"]);
  if (actual.user_id !== actor.userId) {
    throw new Error("Persisted engagement owner does not match the E2E actor.");
  }
}

export async function assertPersistedTask(
  api: APIRequestContext,
  actor: E2EActor,
  expected: TaskRecord,
  input: TaskInput,
  engagement: EngagementRecord,
): Promise<void> {
  const response = await api.get(`/api/tasks/${expected.id}`, {
    headers: actorHeaders(actor),
  });
  const actual = await readSuccessfulJson<TaskRecord>(response, "Task readback");
  assertFieldsEqual(actual, expected, [
    "id",
    "user_id",
    "engagement_id",
    "name",
    "description",
    "scope",
    "status",
  ]);
  if (actual.user_id !== actor.userId) {
    throw new Error("Persisted task owner does not match the E2E actor.");
  }
  if (actual.engagement_id !== engagement.id) {
    throw new Error("Persisted task engagement does not match the UI selection.");
  }
  if (actual.name !== input.name || actual.scope !== (input.scope ?? null)) {
    throw new Error("Persisted task values do not match the UI input.");
  }
}

async function readSuccessfulJson<T>(
  response: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> },
  label: string,
): Promise<T> {
  if (!response.ok()) {
    throw new Error(`${label} failed: ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as T;
}

function assertFieldsEqual<T extends object>(
  actual: T,
  expected: T,
  fields: Array<keyof T>,
): void {
  for (const field of fields) {
    if (actual[field] !== expected[field]) {
      throw new Error(`Persisted ${String(field)} does not match its created value.`);
    }
  }
}

async function openCreateMenuItem(page: Page, itemName: string): Promise<void> {
  await page.getByRole("button", { name: "New", exact: true }).click();
  await page.getByRole("menuitem", { name: itemName }).click();
}

async function waitForEngagementByName(
  api: APIRequestContext,
  actor: E2EActor,
  name: string,
): Promise<EngagementRecord> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const response = await api.get("/api/engagements/?status=active&limit=100", {
      headers: actorHeaders(actor),
    });
    const payload = await readSuccessfulJson<{ items?: EngagementRecord[] }>(
      response,
      "Engagement list",
    );
    const engagement = (payload.items ?? []).find((candidate) => candidate.name === name);
    if (engagement) {
      return engagement;
    }
    await wait(250);
  }
  throw new Error("Created engagement did not appear in authenticated API state.");
}

async function waitForTaskByName(
  api: APIRequestContext,
  actor: E2EActor,
  name: string,
): Promise<TaskRecord> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const response = await api.get("/api/tasks/", { headers: actorHeaders(actor) });
    const tasks = await readSuccessfulJson<TaskRecord[]>(response, "Task list");
    const task = tasks.find((candidate) => candidate.name === name);
    if (task) {
      return task;
    }
    await wait(250);
  }
  throw new Error("Created task did not appear in authenticated API state.");
}

function wait(delayMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
