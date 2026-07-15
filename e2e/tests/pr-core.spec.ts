/**
 * Deterministic PR-core coverage for auth, task, chat, and isolation workflows.
 */

import { expect, request, test, type APIRequestContext } from "@playwright/test";

import { authenticate, ensureSetupReady, installAuthToken } from "../fixtures/auth";
import {
  assertFinalAssistantMessage,
  DETERMINISTIC_FINAL_TEXT,
  pollChatHistoryItems,
  sendDeterministicChatMessage,
} from "../fixtures/chat";
import {
  createTask,
  createTaskThroughUi,
  getTaskResponse,
  listTasks,
  openTaskInChat,
} from "../fixtures/tasks";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";

let frontendBaseUrl = process.env.BASE_URL ?? "http://localhost:5000";
let apiBaseUrl = process.env.API_URL ?? "http://localhost:8000";
const START_BACKEND_IN_TEST = process.env.E2E_START_BACKEND === "true";
const STACK_STARTUP_TIMEOUT_MS = 90_000;

test.setTimeout(STACK_STARTUP_TIMEOUT_MS);

let backendHandle: DeterministicBackendHandle | null = null;

test.beforeAll(async () => {
  test.setTimeout(STACK_STARTUP_TIMEOUT_MS);
  if (!START_BACKEND_IN_TEST) {
    return;
  }
  backendHandle = await startDeterministicSuiteStack({
    startupDelayMs: STACK_STARTUP_TIMEOUT_MS,
    resources: { label: "pr-core" },
  });
  apiBaseUrl = backendHandle.baseUrl;
  frontendBaseUrl = backendHandle.frontendUrl ?? frontendBaseUrl;
});

test.afterAll(async () => {
  if (!START_BACKEND_IN_TEST) {
    return;
  }
  await stopDeterministicBackend(backendHandle);
});

test("authenticates and loads the app shell", {
  tag: ["@pr-core", "@journey"],
}, async ({ page }) => {
  const api = await newApiContext();
  try {
    await ensureSetupReady(api);
    const { token } = await authenticate(api);
    await installAuthToken(page, token);

    await page.goto(frontendBaseUrl, { waitUntil: "domcontentloaded" });

    await expect(page.getByText("Operations").first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("chat-message-list")).toBeVisible({ timeout: 30_000 });
  } finally {
    await api.dispose();
  }
});

test("creates and opens a task", {
  tag: ["@pr-core", "@journey"],
}, async ({ page }) => {
  const api = await newApiContext();
  try {
    await ensureSetupReady(api);
    const { token } = await authenticate(api);
    await installAuthToken(page, token);

    await page.goto(frontendBaseUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("chat-message-list")).toBeVisible({ timeout: 30_000 });

    const taskName = `e2e-smoke-task-${Date.now()}`;
    const task = await createTaskThroughUi(page, api, token, taskName);
    await openTaskInChat(page, task.id);

    await expect(page.getByTestId("chat-message-list")).toBeVisible();
    await expect(page.getByLabel("Select task")).toContainText(`#${task.id}`);
  } finally {
    await api.dispose();
  }
});

test("renders deterministic chat response", {
  tag: ["@pr-core", "@journey"],
}, async ({ page }) => {
  const api = await newApiContext();
  try {
    await ensureSetupReady(api);
    const { token } = await authenticate(api);
    await installAuthToken(page, token);

    await page.goto(frontendBaseUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("chat-message-list")).toBeVisible({ timeout: 30_000 });

    const taskName = `e2e-smoke-chat-${Date.now()}`;
    const task = await createTaskThroughUi(page, api, token, taskName);
    await openTaskInChat(page, task.id);

    await sendDeterministicChatMessage(api, token, task.id, `deterministic-smoke-${Date.now()}`);
    await assertFinalAssistantMessage(page);

    const historyItems = await pollChatHistoryItems(api, token, task.id);
    expect(
      historyItems.filter(
        (item) => item.kind === "assistant" && item.content === DETERMINISTIC_FINAL_TEXT,
      ),
    ).toHaveLength(1);
  } finally {
    await api.dispose();
  }
});

test("blocks another user from the task", {
  tag: ["@pr-core", "@journey"],
}, async () => {
  const api = await newApiContext();
  try {
    await ensureSetupReady(api);
    const userA = await authenticate(api);
    const userB = await authenticate(api);
    const protectedTaskName = `e2e-smoke-private-${Date.now()}`;
    const task = await createTask(api, userA.token, protectedTaskName);

    const visibleToUserB = await listTasks(api, userB.token);
    expect(visibleToUserB.some((candidate) => candidate.id === task.id)).toBe(false);
    expect(visibleToUserB.some((candidate) => candidate.name === protectedTaskName)).toBe(false);

    const crossUserResponse = await getTaskResponse(api, userB.token, task.id);
    expect([403, 404]).toContain(crossUserResponse.status());
    const body = await crossUserResponse.text();
    expect(body).not.toContain(protectedTaskName);
    expect(body).not.toContain("Deterministic core workflow smoke task");
  } finally {
    await api.dispose();
  }
});
async function newApiContext(): Promise<APIRequestContext> {
  return request.newContext({ baseURL: apiBaseUrl });
}
