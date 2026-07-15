/** Live contract for actor, offline membership, and domain fixtures against the real stack. */

import assert from "node:assert/strict";
import test from "node:test";
import { request, type APIRequestContext } from "@playwright/test";

import { createOwnerActor, createViewerActor } from "./actors";
import {
  assertPersistedEngagement,
  assertPersistedTask,
  createEngagement,
  createTaskForEngagement,
} from "./domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "./deterministic-backend";

test("owner/viewer and engagement/task fixtures use real persisted boundaries", async () => {
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;
  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: 90_000,
      resources: { label: "actor-domain-contract" },
    });
    api = await request.newContext({ baseURL: stack.baseUrl });

    const owner = await createOwnerActor(api);
    assert.ok(stack.resources);
    const viewer = await createViewerActor(api, owner, { resources: stack.resources });
    assert.equal(owner.role, "owner");
    assert.equal(viewer.role, "viewer");
    assert.equal(viewer.tenantId, owner.tenantId);
    assert.notEqual(viewer.userId, owner.userId);

    const engagement = await createEngagement(api, owner, {
      name: `fixture-engagement-${Date.now()}`,
      description: "Live typed fixture contract",
    });
    const taskInput = {
      name: `fixture-task-${Date.now()}`,
      description: "Live persisted task fixture",
      scope: "127.0.0.1",
    };
    const taskRecord = await createTaskForEngagement(api, owner, engagement, taskInput);
    await assertPersistedEngagement(api, owner, engagement);
    await assertPersistedTask(api, owner, taskRecord, taskInput, engagement);
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});
