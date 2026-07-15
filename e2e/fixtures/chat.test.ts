/** Contract tests for deterministic chat fixture history requests. */

import assert from "node:assert/strict";
import test from "node:test";

import type { APIRequestContext } from "@playwright/test";

import { pollPersistedStreamEvents } from "./chat";

test("persisted stream polling uses unfiltered replay history and its items cursor", async () => {
  const urls: string[] = [];
  const pages = [
    {
      items: [{ sequence: 1, type: "user_message" }],
      nextAfter: 1,
      hasMore: true,
    },
    {
      items: [{ sequence: 2, type: "assistant_final" }],
      nextAfter: 2,
      hasMore: false,
    },
  ];
  const api = {
    get: async (url: string) => {
      urls.push(url);
      const payload = pages.shift();
      assert.ok(payload);
      return {
        ok: () => true,
        json: async () => payload,
      };
    },
  } as unknown as APIRequestContext;

  const events = await pollPersistedStreamEvents(api, "owner-token", 42);

  assert.deepEqual(urls, [
    "/api/tasks/42/reasoning/replay?after=0&limit=200",
    "/api/tasks/42/reasoning/replay?after=1&limit=200",
  ]);
  assert.deepEqual(events, [
    { sequence: 1, type: "user_message" },
    { sequence: 2, type: "assistant_final" },
  ]);
});
