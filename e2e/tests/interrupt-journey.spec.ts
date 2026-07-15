/** Deterministic owner journey for approval, rejection, clarification, and isolation. */

import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";

import { createOwnerActor, installActorSession } from "../fixtures/actors";
import {
  assertExactLatestActivityDetailOrder,
  assertExactLatestTurnGroupOrder,
  fetchChatHistoryItems,
  pollPersistedTurnItems,
  sendChatMessageThroughUi,
} from "../fixtures/chat";
import { createEngagement, createTaskForEngagement } from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import {
  expectCrossTaskResumeRejected,
  expectDuplicateResumeRejected,
  expectTasksWithoutInterrupt,
  pollInterrupt,
  pollInterruptCleared,
} from "../fixtures/interrupts";
import { openTaskInChat } from "../fixtures/tasks";

const JOURNEY_TIMEOUT_MS = 120_000;

test("handles deterministic interrupts with task-local isolation", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "interrupts" },
    });
    if (!stack.frontendUrl) {
      throw new Error("Interrupt stack did not expose its frontend URL.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    const engagement = await createEngagement(api, owner, { name: `Interrupts ${Date.now()}` });
    const approvalTask = await createTaskForEngagement(api, owner, engagement, {
      name: `Approval ${Date.now()}`,
    });
    const rejectionTask = await createTaskForEngagement(api, owner, engagement, {
      name: `Rejection ${Date.now()}`,
    });
    const clarifyTask = await createTaskForEngagement(api, owner, engagement, {
      name: `Clarification ${Date.now()}`,
    });
    await installActorSession(page, owner);
    await page.goto(stack.frontendUrl, { waitUntil: "domcontentloaded" });

    const approvalPrompt = "deterministic-interrupt-approval";
    await openTaskInChat(page, approvalTask.id);
    await sendInterruptPrompt(page, approvalPrompt);
    const approval = await pollInterrupt(api, owner, approvalTask.id, "tool_approval");
    await expectTasksWithoutInterrupt(api, owner, [rejectionTask.id, clarifyTask.id]);
    await expectCrossTaskResumeRejected(api, owner, rejectionTask.id, approval, { action: "approve" });
    expect((await pollInterrupt(api, owner, approvalTask.id, "tool_approval")).interrupt_id).toBe(
      approval.interrupt_id,
    );
    await page.reload({ waitUntil: "domcontentloaded" });
    await openTaskInChat(page, approvalTask.id);
    await expect(page.getByText("Workspace Read", { exact: true })).toBeVisible();
    await assertExactLatestTurnGroupOrder(page, ["user", "activity", "message"]);
    await assertExactLatestActivityDetailOrder(page, ["reasoning"]);
    const approvalResumeRequests: string[] = [];
    page.on("request", (requestRecord) => {
      if (
        requestRecord.method() === "POST" &&
        requestRecord.url().endsWith(`/api/tasks/${approvalTask.id}/graph/resume`)
      ) {
        approvalResumeRequests.push(requestRecord.url());
      }
    });
    const runButton = page.getByRole("button", { name: "Run", exact: true });
    await runButton.evaluate((button) => {
      (button as HTMLButtonElement).click();
      (button as HTMLButtonElement).click();
    });
    await expect(runButton).toBeHidden();
    await expect.poll(() => approvalResumeRequests.length).toBe(1);
    await pollInterruptCleared(api, owner, approvalTask.id);
    await assertResumedTurn(
      page,
      api,
      owner.token,
      approvalTask.id,
      approvalPrompt,
      "Approved and resumed.",
      ["reasoning", "tool"],
    );
    await expectDuplicateResumeRejected(api, owner, approvalTask.id, approval, { action: "approve" });

    const rejectionPrompt = "deterministic-interrupt-plan-review";
    await openTaskInChat(page, rejectionTask.id);
    await sendInterruptPrompt(page, rejectionPrompt);
    await pollInterrupt(api, owner, rejectionTask.id, "plan_review");
    await expectTasksWithoutInterrupt(api, owner, [approvalTask.id, clarifyTask.id]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await openTaskInChat(page, rejectionTask.id);
    const rejectButton = page.getByRole("button", { name: "Reject", exact: true });
    await expect(rejectButton).toBeVisible();
    await assertExactLatestTurnGroupOrder(page, ["user", "activity", "message"]);
    await assertExactLatestActivityDetailOrder(page, ["reasoning"]);
    await rejectButton.click();
    await pollInterruptCleared(api, owner, rejectionTask.id);
    await assertResumedTurn(
      page,
      api,
      owner.token,
      rejectionTask.id,
      rejectionPrompt,
      "Plan rejected.",
      ["reasoning"],
    );

    const clarifyPrompt = "deterministic-interrupt-clarify";
    await openTaskInChat(page, clarifyTask.id);
    await sendInterruptPrompt(page, clarifyPrompt);
    await pollInterrupt(api, owner, clarifyTask.id, "clarify_request");
    await expectTasksWithoutInterrupt(api, owner, [approvalTask.id, rejectionTask.id]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await openTaskInChat(page, clarifyTask.id);
    await expect(page.getByText("Clarification Required", { exact: true })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Assessment position" })).toContainText("Internal");
    await assertExactLatestTurnGroupOrder(page, ["user", "activity", "message"]);
    await assertExactLatestActivityDetailOrder(page, ["reasoning"]);
    await page.getByRole("button", { name: "Submit Answers" }).click();
    await pollInterruptCleared(api, owner, clarifyTask.id);
    await assertResumedTurn(
      page,
      api,
      owner.token,
      clarifyTask.id,
      clarifyPrompt,
      "Clarification accepted.",
      ["reasoning"],
    );
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

async function sendInterruptPrompt(page: Page, prompt: string): Promise<void> {
  await sendChatMessageThroughUi(page, prompt, { primaryMode: "agent_full", planMode: true });
}

async function assertResumedTurn(
  page: Page,
  api: APIRequestContext,
  token: string,
  taskId: number,
  prompt: string,
  expectedText: string,
  expectedActivityKinds: Array<"reasoning" | "tool" | "observation">,
): Promise<void> {
  const items = await pollPersistedTurnItems(api, token, taskId, prompt);
  expect(items.filter((item) => item.kind === "user")).toHaveLength(1);
  expect(items.filter((item) => item.kind === "assistant")).toHaveLength(1);
  const expectedPersistedActivityKinds = expectedActivityKinds.flatMap((kind) =>
    kind === "reasoning" ? ["reasoning", "reasoning"] : [kind],
  );
  expect(
    items
      .filter((item) => ["reasoning", "tool", "observation"].includes(item.kind))
      .map((item) => item.kind),
  ).toEqual(expectedPersistedActivityKinds);
  await expect.poll(
    async () => (await fetchChatHistoryItems(api, token, taskId))
      .some((item) => item.kind === "assistant" && item.content === expectedText),
    { timeout: 30_000 },
  ).toBe(true);
  await page.reload({ waitUntil: "domcontentloaded" });
  await openTaskInChat(page, taskId);
  await assertExactLatestTurnGroupOrder(page, ["user", "activity", "message"]);
  await assertExactLatestActivityDetailOrder(page, expectedActivityKinds);
  await expect(page.getByTestId("message-bubble-agent").filter({ hasText: expectedText }).last()).toBeVisible();
}
