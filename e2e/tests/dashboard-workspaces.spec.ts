/** Task-local dashboard journey for operations, files, threats, and production-safe task actions. */

import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";

import { actorHeaders, createOwnerActor, installActorSession } from "../fixtures/actors";
import { createEngagement, createTaskForEngagement } from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import { seedWorkspaceKnowledge } from "../fixtures/offline-seed";

const JOURNEY_TIMEOUT_MS = 120_000;

test("keeps dashboard workspaces and previews task-local", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "dashboard-workspaces" },
      extraEnv: { TASK_RUNTIME_PLACEMENT_MODE_DEFAULT: "local" },
    });
    if (!stack.frontendUrl || !stack.resources) {
      throw new Error("Dashboard workspace stack did not expose isolated resources.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    const suffix = Date.now();
    const engagement = await createEngagement(api, owner, { name: `Workspace ${suffix}` });
    const task = await createTaskForEngagement(api, owner, engagement, {
      name: `Workspace task ${suffix}`,
      scope: "192.0.2.0/24",
    });
    const otherEngagement = await createEngagement(api, owner, { name: `Other workspace ${suffix}` });
    const otherTask = await createTaskForEngagement(api, owner, otherEngagement, {
      name: `Other task ${suffix}`,
      scope: "198.51.100.0/24",
    });
    const ownerFilename = `owner-observation-${suffix}.txt`;
    const ownerContent = `task-local-owner-content-${suffix}`;
    const findingTitle = `Exposed deterministic service ${suffix}`;
    const otherFilename = `other-task-private-${suffix}.txt`;
    const otherContent = `other-task-private-content-${suffix}`;
    seedWorkspaceKnowledge({
      resources: stack.resources,
      userId: owner.userId,
      tenantId: owner.tenantId,
      engagementId: engagement.id,
      taskId: task.id,
      relativePath: `artifacts/${ownerFilename}`,
      content: ownerContent,
      findingTitle,
    });
    seedWorkspaceKnowledge({
      resources: stack.resources,
      userId: owner.userId,
      tenantId: owner.tenantId,
      engagementId: otherEngagement.id,
      taskId: otherTask.id,
      relativePath: `artifacts/${otherFilename}`,
      content: otherContent,
      findingTitle: `Other private finding ${suffix}`,
    });
    const ownerTree = await api.get(`/api/tasks/${task.id}/files/tree`, {
      headers: actorHeaders(owner),
    });
    if (!ownerTree.ok()) {
      throw new Error(`Seeded owner file tree failed: ${ownerTree.status()} ${await ownerTree.text()}`);
    }
    const ownerTreePayload = JSON.stringify(await ownerTree.json());
    expect(ownerTreePayload).toContain(ownerFilename);
    expect(ownerTreePayload).not.toContain(otherFilename);

    await installActorSession(page, owner);
    await page.goto(stack.frontendUrl, { waitUntil: "domcontentloaded" });

    await assertOperationsTaskActions(page, task.id, task.name);
    await assertTaskLocalFilePreview(
      page,
      api,
      owner,
      task.id,
      task.name,
      otherTask.id,
      ownerFilename,
      ownerContent,
      otherFilename,
      otherContent,
    );
    await assertThreatDashboard(page, engagement.name, findingTitle, `Other private finding ${suffix}`);
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

async function assertOperationsTaskActions(
  page: Page,
  taskId: number,
  taskName: string,
): Promise<void> {
  await expect(page.getByRole("button", { name: "Operations", exact: true })).toBeVisible();
  const taskCard = page.getByTestId(`task-card-${taskId}`);
  await expect(taskCard.getByRole("heading", { name: taskName })).toBeVisible();
  await taskCard.getByRole("button", { name: `Task actions for ${taskName}` }).click();
  const taskMenu = page.getByRole("menu", { name: `Task actions for ${taskName}` });
  await expect(taskMenu.getByRole("menuitem", { name: "View Details" })).toBeVisible();
  await expect(taskMenu.getByRole("menuitem", { name: "Memory Flow" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(taskMenu).toBeHidden();
}

async function assertTaskLocalFilePreview(
  page: Page,
  api: APIRequestContext,
  owner: Parameters<typeof actorHeaders>[0],
  taskId: number,
  taskName: string,
  otherTaskId: number,
  ownerFilename: string,
  ownerContent: string,
  otherFilename: string,
  otherContent: string,
): Promise<void> {
  await page.getByRole("button", { name: "File Explorer", exact: true }).click();
  await expect(page.getByText("File Explorer", { exact: true }).last()).toBeVisible();
  const taskSelector = page.getByRole("combobox", { name: "Select task" });
  await taskSelector.click();
  await page.getByRole("option", { name: new RegExp(`${escapeRegex(taskName)}.*#${taskId}`) }).click();
  await expect(page.getByText("artifacts", { exact: true })).toBeVisible();
  await page.getByText("artifacts", { exact: true }).click();
  await expect(page.getByText(ownerFilename, { exact: true })).toBeVisible();
  await expect(page.getByText(otherFilename, { exact: true })).toHaveCount(0);
  await page.getByText(ownerFilename, { exact: true }).click();
  await expect(page.getByText(ownerContent, { exact: true })).toBeVisible();
  await expect(page.getByText(otherContent, { exact: true })).toHaveCount(0);

  const traversal = await api.get(`/api/tasks/${taskId}/files/content`, {
    headers: actorHeaders(owner),
    params: { path: `../task-${otherTaskId}/artifacts/${otherFilename}` },
  });
  expect(traversal.status()).toBe(403);
  const crossTaskPath = await api.get(`/api/tasks/${taskId}/files/content`, {
    headers: actorHeaders(owner),
    params: { path: `artifacts/${otherFilename}` },
  });
  expect(crossTaskPath.status()).toBe(404);
  expect(await crossTaskPath.text()).not.toContain(otherContent);
}

async function assertThreatDashboard(
  page: Page,
  engagementName: string,
  findingTitle: string,
  otherFindingTitle: string,
): Promise<void> {
  await page.getByRole("button", { name: "Threat Dashboard", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Threat Dashboard" })).toBeVisible();
  const engagementSelector = page.getByRole("combobox", { name: "Engagement" });
  await engagementSelector.click();
  await page.getByRole("option", { name: engagementName, exact: true }).click();
  await expect(page.getByText(findingTitle, { exact: true })).toBeVisible();
  await expect(page.getByText(otherFindingTitle, { exact: true })).toHaveCount(0);
  await expect(page.getByText("High", { exact: true }).first()).toBeVisible();
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
