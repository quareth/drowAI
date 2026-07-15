/** Typed authenticated owner/viewer identities for isolated browser journeys. */

import type {
  APIRequestContext,
  Browser,
  BrowserContext,
  Page,
} from "@playwright/test";

import {
  authenticate,
  E2E_PASSWORD,
  ensureSetupReady,
  uniqueE2EUsername,
  type AuthResult,
} from "./auth";
import { seedMembership } from "./offline-seed";
import type { E2ESuiteResources } from "./suite-resources";

export type E2EActorRole = "owner" | "viewer";

type BrowserCookie = Awaited<ReturnType<APIRequestContext["storageState"]>>["cookies"][number];

const browserCookiesByAccessToken = new Map<string, BrowserCookie[]>();

export interface E2EActor {
  role: E2EActorRole;
  token: string;
  userId: number;
  username: string;
  password: string;
  tenantId: number;
  membershipId: number;
}

export interface CreateActorOptions {
  username?: string;
  password?: string;
}

export interface CreateViewerOptions extends CreateActorOptions {
  resources: E2ESuiteResources;
  cwd?: string;
}

export interface E2EBrowserSession {
  actor: E2EActor;
  context: BrowserContext;
  page: Page;
  close(): Promise<void>;
}

/** Register/login one owner and resolve its persisted default tenant context. */
export async function createOwnerActor(
  api: APIRequestContext,
  options: CreateActorOptions = {},
): Promise<E2EActor> {
  await ensureSetupReady(api);
  const password = options.password ?? E2E_PASSWORD;
  const auth = await authenticate(
    api,
    options.username ?? uniqueE2EUsername("e2e_owner"),
    password,
  );
  return resolveActor(api, auth, password, "owner");
}

/** Register normally, then downgrade the membership through the offline seed boundary. */
export async function createViewerActor(
  api: APIRequestContext,
  owner: E2EActor,
  options: CreateViewerOptions,
): Promise<E2EActor> {
  const password = options.password ?? E2E_PASSWORD;
  const auth = await authenticate(
    api,
    options.username ?? uniqueE2EUsername("e2e_viewer"),
    password,
  );
  const seeded = seedMembership({
    resources: options.resources,
    actorUserId: owner.userId,
    targetUserId: auth.userId,
    tenantId: owner.tenantId,
    role: "viewer",
    cwd: options.cwd,
  });
  if (seeded.role !== "viewer" || seeded.user_id !== auth.userId) {
    throw new Error("Offline viewer membership seed returned an unexpected identity.");
  }
  return resolveActor(api, auth, password, "viewer", owner.tenantId);
}

/** Install the actor's complete browser session and task-tenant selection. */
export async function installActorSession(page: Page, actor: E2EActor): Promise<void> {
  const cookies = browserCookiesByAccessToken.get(actor.token) ?? [];
  if (cookies.length > 0) {
    await page.context().addCookies(cookies);
  }
  await installActorStorage(page, actor);
}

/** Create a new browser context so each actor has isolated cookies and storage. */
export async function openActorBrowserSession(
  browser: Browser,
  actor: E2EActor,
): Promise<E2EBrowserSession> {
  const context = await browser.newContext();
  await installActorCookies(context, actor);
  await installActorStorage(context, actor);
  const page = await context.newPage();
  return {
    actor,
    context,
    page,
    close: () => context.close(),
  };
}

async function installActorStorage(
  target: Pick<Page, "addInitScript"> | Pick<BrowserContext, "addInitScript">,
  actor: E2EActor,
): Promise<void> {
  await target.addInitScript(
    ({ token, tenantId }) => {
      const markerKey = "drowai:e2e:actor-session-installed";
      if (window.sessionStorage.getItem(markerKey) === "true") {
        return;
      }
      window.localStorage.setItem("access_token", token);
      window.localStorage.setItem("active_tenant_id", String(tenantId));
      window.sessionStorage.setItem(markerKey, "true");
    },
    { token: actor.token, tenantId: actor.tenantId },
  );
}

async function installActorCookies(
  context: Pick<BrowserContext, "addCookies">,
  actor: E2EActor,
): Promise<void> {
  const cookies = browserCookiesByAccessToken.get(actor.token) ?? [];
  if (cookies.length > 0) {
    await context.addCookies(cookies);
  }
}

export function actorHeaders(actor: E2EActor): Record<string, string> {
  return {
    Authorization: `Bearer ${actor.token}`,
    "X-Active-Tenant-Id": String(actor.tenantId),
  };
}

async function resolveActor(
  api: APIRequestContext,
  auth: AuthResult,
  password: string,
  expectedRole: E2EActorRole,
  requestedTenantId?: number,
): Promise<E2EActor> {
  const response = await api.get("/api/auth/me", {
    headers: {
      Authorization: `Bearer ${auth.token}`,
      ...(requestedTenantId
        ? { "X-Active-Tenant-Id": String(requestedTenantId) }
        : {}),
    },
  });
  if (!response.ok()) {
    throw new Error(`Actor context failed: ${response.status()} ${await response.text()}`);
  }
  const payload = (await response.json()) as AuthMePayload;
  if (!payload.active_tenant || payload.active_tenant.role !== expectedRole) {
    throw new Error(`Expected ${expectedRole} tenant context for E2E actor.`);
  }
  const browserCookies = (await api.storageState()).cookies.filter(
    (cookie) => cookie.name === "drowai_refresh_token",
  );
  if (browserCookies.length === 0) {
    throw new Error("Authenticated E2E actor did not receive a refresh-session cookie.");
  }
  browserCookiesByAccessToken.set(auth.token, browserCookies);
  return {
    role: expectedRole,
    token: auth.token,
    userId: auth.userId,
    username: auth.username,
    password,
    tenantId: payload.active_tenant.tenant_id,
    membershipId: payload.active_tenant.membership_id,
  };
}

interface AuthMePayload {
  active_tenant?: {
    tenant_id: number;
    membership_id: number;
    role: string;
  } | null;
}
