/** Owner journey for UI domain creation and deterministic chat. */

import { expect, request, test, type APIRequestContext } from "@playwright/test";

import { createOwnerActor, installActorSession } from "../fixtures/actors";
import {
  assertFinalAssistantMessage,
  assertLatestActivityDetailOrder,
  assertLatestTurnGroupOrder,
  ensureToolOutputVisible,
  expectOrderedStreamTypes,
  expectOrderedTurnKinds,
  pollPersistedStreamEvents,
  pollPersistedTurnItems,
  sendChatMessageThroughUi,
} from "../fixtures/chat";
import {
  assertPersistedEngagement,
  assertPersistedTask,
  createEngagementThroughUi,
  createTaskThroughUiForEngagement,
} from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import { openTaskInChat } from "../fixtures/tasks";

const STARTUP_TIMEOUT_MS = 90_000;

test("runs the persisted owner core journey", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(STARTUP_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: STARTUP_TIMEOUT_MS,
      resources: { label: "owner-core" },
    });
    if (!stack.frontendUrl) {
      throw new Error("Owner-core stack did not expose its frontend URL.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    await installActorSession(page, owner);
    await page.goto(stack.frontendUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByText("Operations").first()).toBeVisible({ timeout: 30_000 });

    const suffix = Date.now();
    const engagement = await createEngagementThroughUi(page, api, owner, {
      name: `Owner journey engagement ${suffix}`,
      description: "Persisted UI engagement for deterministic owner coverage",
    });
    await assertPersistedEngagement(api, owner, engagement);

    const taskInput = {
      name: `Owner journey task ${suffix}`,
      scope: "127.0.0.1",
    };
    const taskRecord = await createTaskThroughUiForEngagement(page, api, owner, engagement, taskInput);
    await assertPersistedTask(api, owner, taskRecord, taskInput, engagement);
    await openTaskInChat(page, taskRecord.id);

    const simplePrompt = `owner-simple-${suffix}`;
    await sendChatMessageThroughUi(page, simplePrompt, {
      primaryMode: "agent_full",
      planMode: false,
    });
    const simpleItems = await pollPersistedTurnItems(api, owner.token, taskRecord.id, simplePrompt);
    expectOrderedTurnKinds(simpleItems, ["user", "assistant"]);
    const simpleStream = await pollPersistedStreamEvents(api, owner.token, taskRecord.id);
    expectOrderedStreamTypes(simpleStream, [
      "user_message",
      "tool_start",
      "tool_end",
      "message_start",
      "message_delta",
      "section_end",
      "message_delta",
      "assistant_final",
    ]);
    await assertFinalAssistantMessage(page);
    await ensureToolOutputVisible(page);
    await assertLatestTurnGroupOrder(page, ["activity", "message"]);

    await page.reload({ waitUntil: "domcontentloaded" });
    await openTaskInChat(page, taskRecord.id);
    await assertFinalAssistantMessage(page);
    await assertLatestActivityDetailOrder(page, ["tool"]);
    await ensureToolOutputVisible(page);

    const deepPrompt = `owner-deep-${suffix}`;
    await sendChatMessageThroughUi(page, deepPrompt, {
      primaryMode: "agent_full",
      planMode: true,
    });
    const deepItems = await pollPersistedTurnItems(api, owner.token, taskRecord.id, deepPrompt);
    expectOrderedTurnKinds(deepItems, ["user", "assistant"]);
    const deepStream = await pollPersistedStreamEvents(api, owner.token, taskRecord.id);
    expectOrderedStreamTypes(deepStream, [
      "user_message",
      "reasoning_start",
      "reasoning_delta",
      "tool_start",
      "tool_end",
      "observation_start",
      "observation_delta",
      "observation_section_end",
      "message_start",
      "message_delta",
      "section_end",
      "assistant_final",
    ]);
    await assertLatestTurnGroupOrder(page, ["activity", "message"]);
    await assertLatestActivityDetailOrder(page, ["reasoning", "tool", "observation"]);
    await ensureToolOutputVisible(page);
    const observationCount = await page.locator("[data-testid^='observation-card-']").count();
    expect(observationCount).toBeGreaterThan(0);

    await page.reload({ waitUntil: "domcontentloaded" });
    await openTaskInChat(page, taskRecord.id);
    await assertFinalAssistantMessage(page);
    await ensureToolOutputVisible(page);
    await expect(page.locator("[data-testid^='observation-card-']")).toHaveCount(observationCount);

  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});
