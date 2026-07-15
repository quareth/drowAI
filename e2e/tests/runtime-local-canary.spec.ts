/** Real local-Docker UI certification for task lifecycle, shell, files, and isolation. */

import {
  expect,
  chromium,
  request,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";

import { actorHeaders, createOwnerActor, installActorSession, type E2EActor } from "../fixtures/actors";
import {
  createEngagementThroughUi,
  createTaskThroughUiForEngagement,
  type EngagementRecord,
  type TaskRecord,
} from "../fixtures/domain-fixtures";
import {
  startDeterministicBackend,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import {
  RuntimeLocalResourceTracker,
  assertRuntimeLocalPlatform,
  assertRuntimeLocalPrerequisites,
} from "../fixtures/runtime-local-contract";
import { allocateSuiteResources, type E2ESuiteResources } from "../fixtures/suite-resources";
import {
  deleteTaskThroughUi,
  expectTaskStatusAfterRefresh,
  runTaskActionThroughUi,
} from "../fixtures/task-lifecycle";

const CANARY_TIMEOUT_MS = 240_000;
const COMMAND_MARKER = "RUNTIME_CANARY_COMMAND_OK";
const ISOLATION_MARKER = "RUNTIME_CANARY_ISOLATED";
const FILE_NAME = "runtime-canary.txt";
const FILE_CONTENT = "runtime-canary-content";

test("certifies the real local-Docker UI lifecycle", { tag: "@runtime-local" }, async () => {
  test.setTimeout(CANARY_TIMEOUT_MS);
  assertRuntimeLocalPlatform();
  const resources = await allocateSuiteResources({ label: "runtime-local" });
  const tracker = new RuntimeLocalResourceTracker(resources);
  let browser: Browser | null = null;
  let context: BrowserContext | null = null;
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    const preflight = await assertRuntimeLocalPrerequisites({ resources });
    browser = await chromium.launch();
    context = await browser.newContext();
    const page = await context.newPage();
    stack = await startDeterministicBackend({
      resources,
      startupDelayMs: 60_000,
      extraEnv: {
        E2E_DETERMINISTIC_MODE: "false",
        E2E_RUNTIME_LOCAL_MODE: "true",
        VITE_E2E_DETERMINISTIC_MODE: "false",
        TASK_RUNTIME_PLACEMENT_MODE_DEFAULT: "local",
        DROWAI_RUNTIME_IMAGE: preflight.image,
      },
    });
    if (!stack.frontendUrl) {
      throw new Error("Runtime-local stack did not expose its frontend URL.");
    }

    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    await installActorSession(page, owner);
    await page.goto(stack.frontendUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("button", { name: "Operations", exact: true })).toBeVisible({
      timeout: 30_000,
    });

    const suffix = Date.now();
    const engagement = await createEngagementThroughUi(page, api, owner, {
      name: `Runtime canary ${suffix}`,
    });
    const firstTask = await createTaskThroughUiForEngagement(page, api, owner, engagement, {
      name: `Runtime primary ${suffix}`,
      scope: "127.0.0.1",
    });
    tracker.trackContainer(`drowai-task-${firstTask.id}`);
    tracker.trackWorkspace(taskWorkspace(resources, firstTask));
    await awaitRuntimeReady(page, api, owner, engagement, firstTask);

    await openShellAndRun(page, firstTask, [
      `mkdir -p /workspace/artifacts && printf '${FILE_CONTENT}' > /workspace/artifacts/${FILE_NAME} && printf '${COMMAND_MARKER}\\n'`,
    ], COMMAND_MARKER);
    await assertFileExplorerPreview(page, firstTask, FILE_NAME, FILE_CONTENT);

    await page.getByRole("button", { name: "Operations", exact: true }).click();
    const secondTask = await createTaskThroughUiForEngagement(page, api, owner, engagement, {
      name: `Runtime isolated ${suffix}`,
      scope: "127.0.0.2",
    });
    tracker.trackContainer(`drowai-task-${secondTask.id}`);
    tracker.trackWorkspace(taskWorkspace(resources, secondTask));
    await awaitRuntimeReady(page, api, owner, engagement, secondTask);

    await openShellAndRun(page, secondTask, [
      `test ! -e /workspace/artifacts/${FILE_NAME} && printf '${ISOLATION_MARKER}\\n'`,
    ], ISOLATION_MARKER);
    await assertFileAbsentFromSecondTask(page, secondTask, FILE_NAME, FILE_CONTENT);

    await page.getByRole("button", { name: "Operations", exact: true }).click();
    await runTaskActionThroughUi(page, api, owner, engagement, firstTask, "Pause", "paused");
    await runTaskActionThroughUi(page, api, owner, engagement, firstTask, "Resume", "running");
    await runTaskActionThroughUi(page, api, owner, engagement, firstTask, "Stop", "stopped");
    await expectRuntimeRemoved(api, owner, firstTask.id);
    await runTaskActionThroughUi(page, api, owner, engagement, secondTask, "Stop", "stopped");
    await expectRuntimeRemoved(api, owner, secondTask.id);
    await expectNoTerminalSessions(api, owner);
    await expect(page.getByTestId("terminal-panel").getByText("No terminals", { exact: true })).toBeVisible();
    await expectTerminalStorageCleared(page, [firstTask.id, secondTask.id]);

    await deleteTaskThroughUi(page, api, owner, engagement, firstTask);
    await deleteTaskThroughUi(page, api, owner, engagement, secondTask);
  } finally {
    await cleanupRuntimeCanary({ api, context, browser, stack, tracker });
  }
});

async function awaitRuntimeReady(
  page: Page,
  api: APIRequestContext,
  owner: E2EActor,
  engagement: EngagementRecord,
  task: TaskRecord,
): Promise<void> {
  await expectTaskStatusAfterRefresh(page, api, owner, engagement, task, "running");
  await expect
    .poll(async () => {
      const response = await api.get(`/api/tasks/${task.id}/container/status`, {
        headers: actorHeaders(owner),
      });
      if (!response.ok()) return `http-${response.status()}`;
      const payload = (await response.json()) as { container_exists?: boolean; status?: string };
      return payload.container_exists && payload.status === "running" ? "ready" : "not-ready";
    }, { timeout: 60_000 })
    .toBe("ready");
}

async function openShellAndRun(
  page: Page,
  task: TaskRecord,
  commands: string[],
  expectedMarker: string,
): Promise<void> {
  await page.getByTestId(`task-card-${task.id}`).getByRole("button", { name: "Shell" }).click();
  await expect(page.getByTestId("terminal-panel")).toBeVisible();
  await expect
    .poll(() => page.evaluate((taskId) => sessionStorage.getItem(`termsid:${taskId}`), task.id), {
      timeout: 30_000,
    })
    .not.toBeNull();

  const output = page.getByTestId("terminal-output");
  await output.click();
  for (const command of commands) {
    await page.keyboard.insertText(command);
    await page.keyboard.press("Enter");
  }
  await expect
    .poll(() => page.evaluate((taskId) => sessionStorage.getItem(`termbuf:${taskId}`) ?? "", task.id), {
      timeout: 30_000,
    })
    .toContain(expectedMarker);
}

async function assertFileExplorerPreview(
  page: Page,
  task: TaskRecord,
  filename: string,
  content: string,
): Promise<void> {
  await page.getByRole("button", { name: "File Explorer", exact: true }).click();
  await selectFileExplorerTask(page, task);
  await page.getByText("artifacts", { exact: true }).click();
  await expect(page.getByText(filename, { exact: true })).toBeVisible();
  await page.getByText(filename, { exact: true }).click();
  await expect(page.getByText(content, { exact: true })).toBeVisible();
}

async function assertFileAbsentFromSecondTask(
  page: Page,
  task: TaskRecord,
  filename: string,
  privateContent: string,
): Promise<void> {
  await page.getByRole("button", { name: "File Explorer", exact: true }).click();
  await selectFileExplorerTask(page, task);
  await expect(page.getByText(filename, { exact: true })).toHaveCount(0);
  await expect(page.getByText(privateContent, { exact: true })).toHaveCount(0);
}

async function selectFileExplorerTask(page: Page, task: TaskRecord): Promise<void> {
  const selector = page.getByRole("combobox", { name: "Select task" });
  await selector.click();
  await page.getByRole("option", { name: new RegExp(`#${task.id}`) }).click();
  await expect(page.getByText("/workspace", { exact: true }).first()).toBeVisible();
}

async function expectRuntimeRemoved(
  api: APIRequestContext,
  owner: E2EActor,
  taskId: number,
): Promise<void> {
  await expect
    .poll(async () => {
      const response = await api.get(`/api/tasks/${taskId}/container/status`, {
        headers: actorHeaders(owner),
      });
      if (!response.ok()) return `http-${response.status()}`;
      const payload = (await response.json()) as { container_exists?: boolean };
      return payload.container_exists === false ? "removed" : "present";
    }, { timeout: 60_000 })
    .toBe("removed");
}

async function expectNoTerminalSessions(api: APIRequestContext, owner: E2EActor): Promise<void> {
  await expect
    .poll(async () => {
      const response = await api.get("/api/docker/terminal/sessions", {
        headers: actorHeaders(owner),
      });
      if (!response.ok()) return -1;
      const payload = (await response.json()) as { total?: number };
      return payload.total ?? -1;
    }, { timeout: 30_000 })
    .toBe(0);
}

async function expectTerminalStorageCleared(page: Page, taskIds: number[]): Promise<void> {
  const stored = await page.evaluate((ids) => ids.map((taskId) => ({
    buffer: sessionStorage.getItem(`termbuf:${taskId}`),
    session: sessionStorage.getItem(`termsid:${taskId}`),
  })), taskIds);
  expect(stored).toEqual(taskIds.map(() => ({ buffer: null, session: null })));
}

function taskWorkspace(resources: E2ESuiteResources, task: TaskRecord): string {
  return `${resources.workspaceRoot}/task-${task.id}`;
}

async function cleanupRuntimeCanary(options: {
  api: APIRequestContext | null;
  context: BrowserContext | null;
  browser: Browser | null;
  stack: DeterministicBackendHandle | null;
  tracker: RuntimeLocalResourceTracker;
}): Promise<void> {
  const failures: unknown[] = [];
  const cleanupSteps = [
    () => options.api?.dispose(),
    () => options.context?.close(),
    () => options.browser?.close(),
    () => stopDeterministicBackend(options.stack),
    () => options.tracker.cleanupAndAssertNoLeaks(),
  ];
  for (const cleanup of cleanupSteps) {
    try {
      await cleanup();
    } catch (error) {
      failures.push(error);
    }
  }
  if (failures.length > 0) {
    throw new AggregateError(failures, "Runtime-local canary cleanup failed");
  }
}
