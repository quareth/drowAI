/** Contract tests for typed E2E actor sessions and authenticated domain builders. */

import assert from "node:assert/strict";
import test from "node:test";
import type { APIRequestContext, Browser, Page } from "@playwright/test";

import {
  actorHeaders,
  createOwnerActor,
  installActorSession,
  openActorBrowserSession,
  type E2EActor,
} from "./actors";
import {
  assertPersistedEngagement,
  assertPersistedTask,
  createEngagement,
  createTaskForEngagement,
} from "./domain-fixtures";

const owner: E2EActor = {
  role: "owner",
  token: "fixture-token",
  userId: 41,
  username: "owner-41",
  password: "fixture-password",
  tenantId: 7,
  membershipId: 11,
};

test("actor headers and browser state include tenant context without password", async () => {
  assert.deepEqual(actorHeaders(owner), {
    Authorization: "Bearer fixture-token",
    "X-Active-Tenant-Id": "7",
  });

  let installedValue: unknown;
  const page = {
    addInitScript: async (_callback: unknown, value: unknown) => {
      installedValue = value;
    },
  } as unknown as Page;
  await installActorSession(page, owner);

  assert.deepEqual(installedValue, { token: "fixture-token", tenantId: 7 });
  assert.equal(JSON.stringify(installedValue).includes(owner.password), false);
});

test("actor browser state is seeded only on the first document in a tab", async () => {
  let initializer: ((value: { token: string; tenantId: number }) => void) | undefined;
  let installedValue: { token: string; tenantId: number } | undefined;
  const page = {
    addInitScript: async (callback: unknown, value: unknown) => {
      initializer = callback as typeof initializer;
      installedValue = value as typeof installedValue;
    },
  } as unknown as Page;
  await installActorSession(page, owner);

  const localValues = new Map<string, string>();
  const sessionValues = new Map<string, string>();
  const storage = (values: Map<string, string>) => ({
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      localStorage: storage(localValues),
      sessionStorage: storage(sessionValues),
    },
  });

  try {
    initializer?.(installedValue!);
    assert.equal(localValues.get("active_tenant_id"), "7");
    localValues.clear();

    initializer?.(installedValue!);
    assert.equal(localValues.get("access_token"), undefined);
    assert.equal(localValues.get("active_tenant_id"), undefined);
  } finally {
    Reflect.deleteProperty(globalThis, "window");
  }
});

test("created actors install their HttpOnly refresh session into the browser context", async () => {
  const refreshCookie = {
    name: "drowai_refresh_token",
    value: "fixture-refresh-secret",
    domain: "127.0.0.1",
    path: "/api/auth",
    expires: -1,
    httpOnly: true,
    secure: false,
    sameSite: "Lax" as const,
  };
  const api = {
    get: async (path: string) => response(
      path === "/api/setup/status"
        ? { setup_required: false, wizard_enabled: true }
        : {
            active_tenant: {
              tenant_id: 7,
              membership_id: 11,
              role: "owner",
            },
          },
    ),
    post: async () => response({
      access_token: "cookie-backed-token",
      user: { id: 41 },
    }),
    storageState: async () => ({ cookies: [refreshCookie], origins: [] }),
  } as unknown as APIRequestContext;
  const actor = await createOwnerActor(api, {
    username: "cookie-owner",
    password: "fixture-password",
  });

  let addedCookies: unknown;
  let installedValue: unknown;
  const page = {
    context: () => ({
      addCookies: async (cookies: unknown) => {
        addedCookies = cookies;
      },
    }),
    addInitScript: async (_callback: unknown, value: unknown) => {
      installedValue = value;
    },
  } as unknown as Page;
  await installActorSession(page, actor);

  assert.deepEqual(addedCookies, [refreshCookie]);
  assert.equal(JSON.stringify(installedValue).includes(refreshCookie.value), false);
});

test("browser actor sessions allocate and close isolated contexts", async () => {
  let closed = false;
  let installedValue: unknown;
  const page = {} as Page;
  const context = {
    addInitScript: async (_callback: unknown, value: unknown) => {
      installedValue = value;
    },
    newPage: async () => page,
    close: async () => {
      closed = true;
    },
  };
  const browser = {
    newContext: async () => context,
  } as unknown as Browser;

  const session = await openActorBrowserSession(browser, owner);
  assert.equal(session.page, page);
  assert.equal(session.actor, owner);
  assert.deepEqual(installedValue, { token: owner.token, tenantId: owner.tenantId });
  await session.close();
  assert.equal(closed, true);
});

test("domain builders send tenant-authenticated writes and verify persisted state", async () => {
  const calls: Array<{ method: string; path: string; options?: Record<string, unknown> }> = [];
  const engagement = {
    id: 101,
    user_id: owner.userId,
    name: "E2E engagement",
    description: "fixture",
    status: "active",
  };
  const task = {
    id: 202,
    user_id: owner.userId,
    engagement_id: engagement.id,
    name: "E2E task",
    description: "fixture task",
    scope: "127.0.0.1",
    status: "created",
  };
  const api = {
    post: async (path: string, options?: Record<string, unknown>) => {
      calls.push({ method: "POST", path, options });
      return response(path === "/api/engagements/" ? engagement : task);
    },
    get: async (path: string, options?: Record<string, unknown>) => {
      calls.push({ method: "GET", path, options });
      return response(path.includes("engagements") ? engagement : task);
    },
  } as unknown as APIRequestContext;

  const createdEngagement = await createEngagement(api, owner, {
    name: engagement.name,
    description: engagement.description,
  });
  const taskInput = {
    name: task.name,
    description: task.description,
    scope: task.scope,
  };
  const createdTask = await createTaskForEngagement(api, owner, createdEngagement, taskInput);
  await assertPersistedEngagement(api, owner, createdEngagement);
  await assertPersistedTask(api, owner, createdTask, taskInput, createdEngagement);

  assert.equal(createdTask.engagement_id, createdEngagement.id);
  for (const call of calls) {
    assert.deepEqual(call.options?.headers, actorHeaders(owner));
  }
  assert.deepEqual((calls[1].options?.data as Record<string, unknown>).engagement_id, engagement.id);
});

function response(payload: unknown) {
  return {
    ok: () => true,
    status: () => 200,
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  };
}
