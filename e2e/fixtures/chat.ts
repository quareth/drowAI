/**
 * Deterministic chat helpers for Playwright smoke tests.
 *
 * The helpers submit deterministic chat turns through the backend and assert
 * the browser-rendered final assistant state without relying on real LLMs.
 */

import { expect, type APIRequestContext, type Page } from "@playwright/test";

const STREAM_POLL_ATTEMPTS = 12;
const STREAM_POLL_DELAY_MS = 1_000;

export const DETERMINISTIC_FINAL_TEXT = "Done.";

export interface ChatHistoryItemRecord {
  id: string;
  kind: string;
  turnNumber: number;
  content: string;
}

export interface ChatStreamEventRecord {
  sequence: number;
  type: string;
}

export interface UiChatMode {
  primaryMode: "chat" | "agent" | "agent_full";
  planMode: boolean;
}

export async function sendDeterministicChatMessage(
  api: APIRequestContext,
  token: string,
  taskId: number,
  message: string,
): Promise<void> {
  const response = await api.post(`/api/tasks/${taskId}/chat`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      message,
      deterministic: true,
    },
  });
  if (!response.ok()) {
    throw new Error(`Deterministic chat send failed: ${response.status()} ${await response.text()}`);
  }
}

export async function assertFinalAssistantMessage(page: Page): Promise<void> {
  const messageList = page.getByTestId("chat-message-list");
  await expect(messageList).toBeVisible({ timeout: 30_000 });
  await expect(
    page.getByTestId("message-bubble-agent").filter({ hasText: DETERMINISTIC_FINAL_TEXT }).last(),
  ).toBeVisible({ timeout: 30_000 });
}

export async function sendChatMessageThroughUi(
  page: Page,
  message: string,
  mode: UiChatMode,
): Promise<void> {
  const modeSwitcher = page.getByTestId("chat-mode-switcher");
  await expect(modeSwitcher).toBeVisible();
  if ((await modeSwitcher.textContent())?.trim() !== primaryModeLabel(mode.primaryMode)) {
    await modeSwitcher.click();
    await page.getByTestId(`chat-mode-option-${mode.primaryMode}`).click();
  }
  await expect(modeSwitcher).toContainText(primaryModeLabel(mode.primaryMode));

  const planToggle = page.getByTestId("chat-plan-toggle");
  const isPressed = (await planToggle.getAttribute("aria-pressed")) === "true";
  if (isPressed !== mode.planMode) {
    await planToggle.click();
  }
  await expect(planToggle).toHaveAttribute("aria-pressed", String(mode.planMode));

  const input = page.getByTestId("chat-input");
  const sendButton = page.getByTestId("chat-send");
  await expect(async () => {
    await expect(input).toBeEnabled();
    await input.fill(message);
    await expect(input).toHaveValue(message);
    await expect(sendButton).toBeEnabled();
  }).toPass({ timeout: 30_000 });
  await sendButton.click();
}

export async function pollPersistedTurnItems(
  api: APIRequestContext,
  token: string,
  taskId: number,
  expectedUserContent?: string,
): Promise<ChatHistoryItemRecord[]> {
  for (let attempt = 0; attempt < STREAM_POLL_ATTEMPTS; attempt += 1) {
    const history = await fetchChatHistoryItems(api, token, taskId);
    const items = expectedUserContent
      ? turnItemsForUser(history, expectedUserContent)
      : latestTurnItems(history);
    if (items.some((item) => item.kind === "assistant")) {
      return items;
    }
    await new Promise((resolve) => setTimeout(resolve, STREAM_POLL_DELAY_MS));
  }
  const history = await fetchChatHistoryItems(api, token, taskId);
  const items = expectedUserContent
    ? turnItemsForUser(history, expectedUserContent)
    : latestTurnItems(history);
  if (!items.some((item) => item.kind === "assistant")) {
    throw new Error(
      `Deterministic turn did not persist an assistant item: ${items.map((item) => item.kind).join(" -> ")}`,
    );
  }
  return items;
}

export function expectOrderedTurnKinds(items: ChatHistoryItemRecord[], expected: string[]): void {
  const actual = items.map((item) => item.kind);
  let cursor = 0;
  for (const eventType of actual) {
    if (eventType === expected[cursor]) {
      cursor += 1;
      if (cursor === expected.length) {
        break;
      }
    }
  }
  expect(cursor, `Expected ordered items ${expected.join(" -> ")}; received ${actual.join(" -> ")}`).toBe(
    expected.length,
  );
  expect(actual.filter((kind) => kind === "assistant")).toHaveLength(1);
}

/** Assert the persisted raw stream retained one ordered, terminal assistant event. */
export function expectOrderedStreamTypes(
  events: ChatStreamEventRecord[],
  expected: string[],
): void {
  const actual = latestStreamTurn(events).map((event) => event.type);
  expectOrderedSequence(actual, expected, "stream events");
  expect(actual.filter((type) => type === "assistant_final")).toHaveLength(1);
}

/** Poll the persisted stream-history boundary until the latest turn completes. */
export async function pollPersistedStreamEvents(
  api: APIRequestContext,
  token: string,
  taskId: number,
): Promise<ChatStreamEventRecord[]> {
  for (let attempt = 0; attempt < STREAM_POLL_ATTEMPTS; attempt += 1) {
    const events = await fetchPersistedStreamEvents(api, token, taskId);
    if (latestStreamTurn(events).some((event) => event.type === "assistant_final")) {
      return events;
    }
    await new Promise((resolve) => setTimeout(resolve, STREAM_POLL_DELAY_MS));
  }
  return fetchPersistedStreamEvents(api, token, taskId);
}

export async function ensureToolOutputVisible(page: Page): Promise<void> {
  await expect(async () => {
    const activityCard = page.locator("[data-testid^='turn-activity-card-']").last();
    if ((await activityCard.count()) > 0 && (await activityCard.isVisible())) {
      const activityToggle = activityCard.locator(":scope > button").first();
      if ((await activityToggle.getAttribute("aria-expanded")) !== "true") {
        await activityToggle.click();
      }
    }

    const toolCard = page.locator("[data-testid^='tool-batch-card-']").last();
    await expect(toolCard).toBeVisible();
    await expect(toolCard).toContainText(/completed|success/i);
    const toggle = toolCard.getByRole("button", { name: "Toggle tool output" }).first();
    if (await toggle.isEnabled()) {
      if ((await toggle.getAttribute("aria-expanded")) !== "true") {
        await toggle.click();
      }
      await expect(toolCard.locator("[data-testid$='-terminal'], p").last()).toBeVisible();
    }
  }).toPass({ timeout: 30_000 });
}

export async function assertLatestTurnGroupOrder(page: Page, expected: string[]): Promise<void> {
  await expect
    .poll(
      async () => {
        const actual = await page.locator("[data-group-type]").evaluateAll((nodes) => {
          const groups = nodes
            .map((node) => ({
              type: node.getAttribute("data-group-type"),
              sequence: Number(node.getAttribute("data-turn-sequence") ?? 0),
            }))
            .filter((group) => group.type && Number.isFinite(group.sequence));
          const latest = groups.reduce((maximum, group) => Math.max(maximum, group.sequence), 0);
          return groups
            .filter((group) => group.sequence === latest)
            .map((group) => group.type as string);
        });
        let previousIndex = -1;
        for (const groupType of expected) {
          const index = actual.indexOf(groupType);
          if (index <= previousIndex) return actual.join(" -> ");
          previousIndex = index;
        }
        return "ordered";
      },
      { timeout: 30_000 },
    )
    .toBe("ordered");
}

export async function assertExactLatestTurnGroupOrder(page: Page, expected: string[]): Promise<void> {
  await expect
    .poll(
      async () => latestTurnGroupTypes(page),
      { timeout: 30_000 },
    )
    .toEqual(expected);
}

export async function assertLatestActivityDetailOrder(
  page: Page,
  expected: Array<"reasoning" | "tool" | "observation">,
): Promise<void> {
  const activityCard = page.locator("[data-testid^='turn-activity-card-']").last();
  await expect(activityCard).toBeVisible({ timeout: 30_000 });
  const activityToggle = activityCard.locator(":scope > button").first();
  if ((await activityToggle.getAttribute("aria-expanded")) !== "true") {
    await activityToggle.click();
  }
  const details = activityCard.locator("[data-testid^='turn-activity-details-']");
  await expect(details).toBeVisible();
  const actual = await details.locator("[data-testid]").evaluateAll((nodes) => {
    const kinds: string[] = [];
    for (const node of nodes) {
      const testId = node.getAttribute("data-testid") ?? "";
      const kind = testId.startsWith("reasoning-step-")
        ? "reasoning"
        : testId.startsWith("tool-batch-card-")
          ? "tool"
          : testId.startsWith("observation-card-")
            ? "observation"
            : null;
      if (kind && kinds[kinds.length - 1] !== kind) {
        kinds.push(kind);
      }
    }
    return kinds;
  });
  let previousIndex = -1;
  for (const kind of expected) {
    const index = actual.indexOf(kind);
    expect(index, `Missing ordered ${kind} detail from ${actual.join(" -> ")}`).toBeGreaterThan(
      previousIndex,
    );
    previousIndex = index;
  }
}

export async function assertExactLatestActivityDetailOrder(
  page: Page,
  expected: Array<"reasoning" | "tool" | "observation">,
): Promise<void> {
  const activityCard = page.locator("[data-testid^='turn-activity-card-']").last();
  await expect(activityCard).toBeVisible({ timeout: 30_000 });
  const activityToggle = activityCard.locator(":scope > button").first();
  if ((await activityToggle.getAttribute("aria-expanded")) !== "true") {
    await activityToggle.click();
  }
  await expect
    .poll(
      async () => activityDetailKinds(activityCard),
      { timeout: 30_000 },
    )
    .toEqual(expected);
}

async function latestTurnGroupTypes(page: Page): Promise<string[]> {
  return page.locator("[data-group-type]").evaluateAll((nodes) => {
    const groups = nodes
      .map((node) => ({
        type: node.getAttribute("data-group-type"),
        sequence: Number(node.getAttribute("data-turn-sequence") ?? 0),
      }))
      .filter((group) => group.type && Number.isFinite(group.sequence));
    const latest = groups.reduce((maximum, group) => Math.max(maximum, group.sequence), 0);
    return groups
      .filter((group) => group.sequence === latest)
      .map((group) => group.type as string);
  });
}

async function activityDetailKinds(activityCard: ReturnType<Page["locator"]>): Promise<string[]> {
  const details = activityCard.locator("[data-testid^='turn-activity-details-']");
  if (!(await details.isVisible())) return [];
  return details.locator("[data-testid]").evaluateAll((nodes) => {
    const kinds: string[] = [];
    for (const node of nodes) {
      const testId = node.getAttribute("data-testid") ?? "";
      const kind = testId.startsWith("reasoning-step-")
        ? "reasoning"
        : testId.startsWith("tool-batch-card-")
          ? "tool"
          : testId.startsWith("observation-card-")
            ? "observation"
            : null;
      if (kind && kinds[kinds.length - 1] !== kind) kinds.push(kind);
    }
    return kinds;
  });
}

export async function pollChatHistoryItems(
  api: APIRequestContext,
  token: string,
  taskId: number,
): Promise<ChatHistoryItemRecord[]> {
  for (let attempt = 0; attempt < STREAM_POLL_ATTEMPTS; attempt += 1) {
    const items = await fetchChatHistoryItems(api, token, taskId);
    if (items.some((item) => item.kind === "assistant" && item.content === DETERMINISTIC_FINAL_TEXT)) {
      return items;
    }
    await new Promise((resolve) => setTimeout(resolve, STREAM_POLL_DELAY_MS));
  }
  return fetchChatHistoryItems(api, token, taskId);
}

export async function fetchChatHistoryItems(
  api: APIRequestContext,
  token: string,
  taskId: number,
): Promise<ChatHistoryItemRecord[]> {
  const response = await api.get(`/api/tasks/${taskId}/chat/history?limit=200`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok()) {
    throw new Error(`Chat history fetch failed: ${response.status()} ${await response.text()}`);
  }
  const payload = (await response.json()) as {
    items?: Array<Record<string, unknown>>;
  };
  return (payload.items ?? [])
    .map((itemPayload) => normalizeChatHistoryItem(itemPayload))
    .filter((record): record is ChatHistoryItemRecord => record !== null);
}

async function fetchPersistedStreamEvents(
  api: APIRequestContext,
  token: string,
  taskId: number,
): Promise<ChatStreamEventRecord[]> {
  const events: ChatStreamEventRecord[] = [];
  let after = 0;
  for (let page = 0; page < 20; page += 1) {
    const response = await api.get(
      `/api/tasks/${taskId}/reasoning/replay?after=${after}&limit=200`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (!response.ok()) {
      throw new Error(`Stream history fetch failed: ${response.status()} ${await response.text()}`);
    }
    const payload = (await response.json()) as {
      items?: Array<Record<string, unknown>>;
      nextAfter?: number;
      hasMore?: boolean;
    };
    for (const eventPayload of payload.items ?? []) {
      const record = normalizeStreamEvent(eventPayload);
      if (record) events.push(record);
    }
    const nextAfter = typeof payload.nextAfter === "number" ? payload.nextAfter : after;
    if (!payload.hasMore || nextAfter <= after) break;
    after = nextAfter;
  }
  return events;
}

function normalizeStreamEvent(
  eventPayload: Record<string, unknown>,
): ChatStreamEventRecord | null {
  const obj = isRecord(eventPayload.obj) ? eventPayload.obj : eventPayload;
  const metadata = isRecord(obj.metadata)
    ? obj.metadata
    : isRecord(eventPayload.metadata)
      ? eventPayload.metadata
      : {};
  const type = typeof obj.type === "string" ? obj.type : null;
  const sequence =
    typeof eventPayload.sequence === "number"
      ? eventPayload.sequence
      : typeof metadata.sequence === "number"
        ? metadata.sequence
        : null;
  return type && sequence !== null ? { type, sequence } : null;
}

function latestStreamTurn(events: ChatStreamEventRecord[]): ChatStreamEventRecord[] {
  const sorted = [...events].sort((left, right) => left.sequence - right.sequence);
  const latestUserIndex = sorted.map((event) => event.type).lastIndexOf("user_message");
  return latestUserIndex < 0 ? sorted : sorted.slice(latestUserIndex);
}

function expectOrderedSequence(actual: string[], expected: string[], label: string): void {
  let cursor = 0;
  for (const item of actual) {
    if (item === expected[cursor]) cursor += 1;
    if (cursor === expected.length) break;
  }
  expect(
    cursor,
    `Expected ordered ${label} ${expected.join(" -> ")}; received ${actual.join(" -> ")}`,
  ).toBe(expected.length);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function normalizeChatHistoryItem(itemPayload: Record<string, unknown>): ChatHistoryItemRecord | null {
  const id = typeof itemPayload.id === "string" ? itemPayload.id : null;
  const kind = typeof itemPayload.kind === "string" ? itemPayload.kind : null;
  const content = typeof itemPayload.content === "string" ? itemPayload.content : null;
  const turnNumber = typeof itemPayload.turn_number === "number" ? itemPayload.turn_number : null;
  if (!id || !kind || content === null || turnNumber === null) {
    return null;
  }
  return { id, kind, turnNumber, content };
}

function latestTurnItems(items: ChatHistoryItemRecord[]): ChatHistoryItemRecord[] {
  const latestTurn = items.reduce((maximum, item) => Math.max(maximum, item.turnNumber), 0);
  return items.filter((item) => item.turnNumber === latestTurn);
}

function turnItemsForUser(
  items: ChatHistoryItemRecord[],
  expectedUserContent: string,
): ChatHistoryItemRecord[] {
  const userItem = [...items]
    .reverse()
    .find((item) => item.kind === "user" && item.content === expectedUserContent);
  return userItem ? items.filter((item) => item.turnNumber === userItem.turnNumber) : [];
}

function primaryModeLabel(mode: UiChatMode["primaryMode"]): string {
  if (mode === "chat") {
    return "Chat";
  }
  if (mode === "agent_full") {
    return "Agent (Full Access)";
  }
  return "Agent";
}
