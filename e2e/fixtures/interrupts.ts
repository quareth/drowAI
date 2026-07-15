/** Authenticated interrupt polling and isolation helpers for Playwright journeys. */

import { expect, type APIRequestContext } from "@playwright/test";

import { actorHeaders, type E2EActor } from "./actors";

const POLL_ATTEMPTS = 40;
const POLL_DELAY_MS = 250;

export type InterruptType = "tool_approval" | "plan_review" | "clarify_request";

export interface InterruptSnapshot {
  has_interrupt: boolean;
  task_id: number;
  interrupt_id?: string | null;
  interrupt_type?: InterruptType | null;
  graph_name?: string | null;
  payload?: Record<string, unknown> | null;
}

export async function pollInterrupt(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
  expectedType: InterruptType,
): Promise<InterruptSnapshot> {
  for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
    const snapshot = await fetchInterrupt(api, actor, taskId);
    if (snapshot.has_interrupt && snapshot.interrupt_type === expectedType) {
      return snapshot;
    }
    await wait();
  }
  const snapshot = await fetchInterrupt(api, actor, taskId);
  throw new Error(
    `Task ${taskId} interrupt remained ${snapshot.interrupt_type ?? "none"}; expected ${expectedType}.`,
  );
}

export async function pollInterruptCleared(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
): Promise<void> {
  for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
    if (!(await fetchInterrupt(api, actor, taskId)).has_interrupt) {
      return;
    }
    await wait();
  }
  throw new Error(`Task ${taskId} interrupt did not clear.`);
}

export async function expectTasksWithoutInterrupt(
  api: APIRequestContext,
  actor: E2EActor,
  taskIds: number[],
): Promise<void> {
  for (const taskId of taskIds) {
    expect((await fetchInterrupt(api, actor, taskId)).has_interrupt).toBe(false);
  }
}

export async function expectCrossTaskResumeRejected(
  api: APIRequestContext,
  actor: E2EActor,
  targetTaskId: number,
  snapshot: InterruptSnapshot,
  response: Record<string, unknown>,
): Promise<void> {
  const result = await resume(api, actor, targetTaskId, snapshot, response);
  expect([404, 409]).toContain(result.status());
}

export async function expectDuplicateResumeRejected(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
  snapshot: InterruptSnapshot,
  response: Record<string, unknown>,
): Promise<void> {
  const result = await resume(api, actor, taskId, snapshot, response);
  expect(result.status()).toBe(409);
}

async function fetchInterrupt(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
): Promise<InterruptSnapshot> {
  const response = await api.get(`/api/tasks/${taskId}/interrupt`, {
    headers: actorHeaders(actor),
  });
  if (!response.ok()) {
    throw new Error(`Interrupt read failed: ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as InterruptSnapshot;
}

function resume(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
  snapshot: InterruptSnapshot,
  response: Record<string, unknown>,
) {
  if (!snapshot.interrupt_id || !snapshot.interrupt_type) {
    throw new Error("Interrupt snapshot lacks resume identity.");
  }
  return api.post(`/api/tasks/${taskId}/graph/resume`, {
    headers: actorHeaders(actor),
    data: {
      interrupt_id: snapshot.interrupt_id,
      interrupt_type: snapshot.interrupt_type,
      graph_name: snapshot.graph_name ?? undefined,
      response,
    },
  });
}

function wait(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, POLL_DELAY_MS));
}
