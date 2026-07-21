/** Full deterministic coverage for remaining pages and engagement cleanup. */

import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";

import { actorHeaders, createOwnerActor, installActorSession, type E2EActor } from "../fixtures/actors";
import {
  createEngagement,
  createTaskForEngagement,
  type EngagementRecord,
  type TaskRecord,
} from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import { seedUsageSettings, usageSettingsCredentialSecret } from "../fixtures/offline-seed";
import { deleteTaskThroughUi, runTaskActionThroughUi } from "../fixtures/task-lifecycle";

const JOURNEY_TIMEOUT_MS = 150_000;
const SETTINGS_SECTIONS = [
  ["API", "Reporting model"],
  ["Network", "Network overview"],
  ["Runner Sites", "Management URL"],
  ["System", "System overview"],
  ["Data Management", "Historical report retention days"],
  ["Display", "Display Preferences"],
  ["CVE", "CVE Indexing"],
] as const;

test("covers Usage, Profile, Settings, and engagement archive lifecycle", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "remaining-pages-lifecycle" },
    });
    if (!stack.frontendUrl || !stack.resources) {
      throw new Error("Remaining-pages journey stack did not expose isolated resources.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    const suffix = Date.now();
    const engagement = await createEngagement(api, owner, {
      name: `Lifecycle engagement ${suffix}`,
    });
    const task = await createTaskForEngagement(api, owner, engagement, {
      name: `Usage lifecycle task ${suffix}`,
      scope: "e2e://remaining-pages",
    });
    const decoyTask = await createTaskForEngagement(api, owner, engagement, {
      name: `Usage filter decoy ${suffix}`,
      scope: "e2e://usage-decoy",
    });
    const conversationId = `e2e-usage-${suffix}`;
    const credentialSecret = usageSettingsCredentialSecret(owner.userId, task.id);
    const seeded = seedUsageSettings({
      resources: stack.resources,
      userId: owner.userId,
      tenantId: owner.tenantId,
      taskId: task.id,
      conversationId,
    });
    expect(seeded.call_count).toBe(2);
    expect(seeded.credential_masked).toBe(true);

    await installActorSession(page, owner);
    await verifyUsagePage(page, api, owner, stack.frontendUrl, task.id, task.name, conversationId);
    await verifyProfilePage(page, stack.frontendUrl, owner);
    await verifySettingsPage(page, api, owner, stack.frontendUrl, credentialSecret);
    await verifyEngagementLifecycleAndCleanup(
      page,
      api,
      owner,
      stack.frontendUrl,
      engagement,
      [task, decoyTask],
    );
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

async function verifyUsagePage(
  page: Page,
  api: APIRequestContext,
  owner: E2EActor,
  frontendUrl: string,
  taskId: number,
  taskName: string,
  conversationId: string,
): Promise<void> {
  await page.goto(`${frontendUrl}/usage`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Usage", exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "Select task" }).click();
  await page.getByRole("option", { name: new RegExp(escapeRegex(taskName)) }).click();
  await expect(page.getByTestId("usage-insights-panel")).toHaveAttribute("data-task-id", String(taskId));
  await expect(page.getByText("160 in", { exact: true })).toBeVisible();
  await expect(page.getByText("60 out", { exact: true })).toBeVisible();
  await expect(page.getByText("2", { exact: true }).last()).toBeVisible();

  const overview = await readJson<{ call_count: number; prompt_tokens: number; completion_tokens: number }>(
    await api.get(`/api/tasks/${taskId}/usage/insights/overview`, { headers: actorHeaders(owner) }),
    "Usage overview",
  );
  expect(overview).toMatchObject({ call_count: 2, prompt_tokens: 160, completion_tokens: 60 });

  await page.getByLabel("Conversation ID").fill(conversationId);
  await page.getByLabel("Conversation ID").press("Enter");
  await expect(page.getByText("120 in", { exact: true })).toBeVisible();
  await expect(page.getByText("40 out", { exact: true })).toBeVisible();
  const filtered = await readJson<{ call_count: number; prompt_tokens: number }>(
    await api.get(
      `/api/tasks/${taskId}/usage/insights/overview?conversation_id=${encodeURIComponent(conversationId)}`,
      { headers: actorHeaders(owner) },
    ),
    "Filtered usage overview",
  );
  expect(filtered).toMatchObject({ call_count: 1, prompt_tokens: 120 });
}

async function verifyProfilePage(page: Page, frontendUrl: string, owner: E2EActor): Promise<void> {
  await page.goto(`${frontendUrl}/profile`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Profile" })).toBeVisible();
  await expect(page.getByText(owner.username, { exact: true }).first()).toBeVisible();
  await page.getByRole("tab", { name: "Access" }).click();
  await expect(page.getByText("Access Context", { exact: true })).toBeVisible();
  await expect(page.getByText("Owner", { exact: true }).first()).toBeVisible();
  await page.getByRole("tab", { name: "Security" }).click();
  await expect(page.getByText("Password Management", { exact: true })).toBeVisible();
}

async function verifySettingsPage(
  page: Page,
  api: APIRequestContext,
  owner: E2EActor,
  frontendUrl: string,
  credentialSecret: string,
): Promise<void> {
  await page.goto(`${frontendUrl}/settings`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  for (const [tabName, visibleText] of SETTINGS_SECTIONS) {
    await page.getByRole("tab", { name: tabName, exact: true }).click();
    await expect(page.getByText(visibleText, { exact: true }).first()).toBeVisible({ timeout: 30_000 });
  }

  await page.getByRole("tab", { name: "API", exact: true }).click();
  await expect(page.getByText("Stored key:").first()).toContainText("***");
  await expect(page.getByLabel("API Key").first()).toHaveValue("");
  if ((await page.locator("body").innerText()).includes(credentialSecret)) {
    throw new Error("Settings UI exposed raw suite credential material.");
  }
  const credentialResponse = await api.get("/api/llm/providers/openai/credential", {
    headers: actorHeaders(owner),
  });
  const credentialBody = await credentialResponse.text();
  expect(credentialResponse.ok()).toBe(true);
  if (credentialBody.includes(credentialSecret)) {
    throw new Error("Credential API exposed raw suite credential material.");
  }
  expect(JSON.parse(credentialBody)).toMatchObject({
    enabled: true,
    has_api_key: true,
    masked_api_key: "***",
  });

  await page.getByRole("tab", { name: "Display", exact: true }).click();
  const timezoneSelect = page.getByRole("combobox", { name: "Timezone" });
  await timezoneSelect.click();
  await page.getByRole("option", { name: "Istanbul", exact: true }).click();
  await expect(timezoneSelect).toHaveText("Istanbul");
  const userSettings = await readJson<{ timezone: string }>(
    await api.get("/api/settings/", { headers: actorHeaders(owner) }),
    "User settings",
  );
  expect(userSettings.timezone).toBe("Europe/Istanbul");

  await page.getByRole("tab", { name: "Data Management", exact: true }).click();
  await page.getByLabel("Historical report retention days").fill("91");
  await page.getByRole("button", { name: "Save policy" }).click();
  await expect(page.getByText("Data management settings updated", { exact: true })).toBeVisible();
  const retention = await readJson<{ report_history_retention_days: number }>(
    await api.get("/api/settings/data-management", { headers: actorHeaders(owner) }),
    "Data management settings",
  );
  expect(retention.report_history_retention_days).toBe(91);

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByLabel("Historical report retention days")).toHaveValue("91");
}

async function verifyEngagementLifecycleAndCleanup(
  page: Page,
  api: APIRequestContext,
  owner: E2EActor,
  frontendUrl: string,
  engagement: EngagementRecord,
  tasksToDelete: TaskRecord[],
): Promise<void> {
  await page.goto(frontendUrl, { waitUntil: "domcontentloaded" });
  await expect(page.getByText(engagement.name, { exact: true }).first()).toBeVisible();

  for (const task of tasksToDelete) {
    await runTaskActionThroughUi(page, api, owner, engagement, task, "Stop", "stopped");
  }

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Engagement actions for ${engagement.name}` }).click();
  await page.getByRole("menuitem", { name: "Archive Engagement" }).click();
  await expectEngagementStatus(api, owner, engagement.id, "archived");

  await page.getByRole("button", { name: "Show archived" }).click();
  await expect(page.getByText(engagement.name, { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: `Engagement actions for ${engagement.name}` }).click();
  await page.getByRole("menuitem", { name: "Restore Engagement" }).click();
  await expectEngagementStatus(api, owner, engagement.id, "active");

  for (const task of tasksToDelete) {
    await deleteTaskThroughUi(page, api, owner, engagement, task);
  }
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Engagement actions for ${engagement.name}` }).click();
  await page.getByRole("menuitem", { name: "Archive Engagement" }).click();
  await expectEngagementStatus(api, owner, engagement.id, "archived");

  const tasks = await readJson<Array<{ id: number }>>(
    await api.get("/api/tasks/", { headers: actorHeaders(owner) }),
    "Task cleanup list",
  );
  const deletedIds = new Set(tasksToDelete.map((task) => task.id));
  expect(tasks.some((candidate) => deletedIds.has(candidate.id))).toBe(false);
}

async function expectEngagementStatus(
  api: APIRequestContext,
  owner: E2EActor,
  engagementId: number,
  expectedStatus: string,
): Promise<void> {
  await expect.poll(async () => {
    const engagement = await readJson<{ status: string }>(
      await api.get(`/api/engagements/${engagementId}`, { headers: actorHeaders(owner) }),
      "Engagement readback",
    );
    return engagement.status;
  }).toBe(expectedStatus);
}

async function readJson<T>(
  response: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> },
  label: string,
): Promise<T> {
  if (!response.ok()) {
    throw new Error(`${label} failed with status ${response.status()}.`);
  }
  return await response.json() as T;
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
