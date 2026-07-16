/** Regression contracts for task lifecycle polling at its final read boundary. */

import assert from "node:assert/strict";
import test from "node:test";

import type { APIRequestContext } from "@playwright/test";

import type { E2EActor } from "./actors";
import type { TaskRecord } from "./domain-fixtures";
import { waitForTaskStatus } from "./task-lifecycle";

const actor: E2EActor = {
  role: "owner",
  token: "synthetic-test-token",
  userId: 1,
  username: "polling-test-owner",
  password: "synthetic-test-password",
  tenantId: 1,
  membershipId: 1,
};

test("accepts the expected task status on the final allowed read", async () => {
  const statuses = ["starting", "running"];
  let readCount = 0;
  const api = {
    get: async () => {
      const status = statuses[Math.min(readCount, statuses.length - 1)];
      readCount += 1;
      const task: TaskRecord = {
        id: 7,
        user_id: actor.userId,
        engagement_id: 11,
        name: "Polling boundary",
        status,
      };
      return {
        ok: () => true,
        json: async () => task,
      };
    },
  } as unknown as APIRequestContext;

  const task = await waitForTaskStatus(api, actor, 7, "running", {
    maxDelays: 1,
    delayMs: 0,
  });

  assert.equal(task.status, "running");
  assert.equal(readCount, 2);
});
