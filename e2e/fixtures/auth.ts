/**
 * Authentication helpers for deterministic Playwright smoke tests.
 *
 * These helpers own API-level user setup and browser token installation so
 * smoke specs can focus on user-visible workflows.
 */

import type { APIRequestContext, Page } from "@playwright/test";

export interface AuthResult {
  token: string;
  userId: number;
  username: string;
}

export const E2E_PASSWORD = process.env.E2E_PASSWORD ?? "e2e_password_123";

export function uniqueE2EUsername(prefix: string): string {
  const worker = process.env.TEST_WORKER_INDEX ?? "0";
  return `${prefix}_${Date.now()}_${worker}_${Math.random().toString(36).slice(2, 8)}`;
}

export async function ensureSetupReady(api: APIRequestContext): Promise<void> {
  const statusResponse = await api.get("/api/setup/status");
  if (!statusResponse.ok()) {
    throw new Error(`Setup status failed: ${statusResponse.status()} ${await statusResponse.text()}`);
  }
  const statusPayload = (await statusResponse.json()) as {
    setup_required?: boolean;
    wizard_enabled?: boolean;
  };
  if (!statusPayload.setup_required || !statusPayload.wizard_enabled) {
    return;
  }

  const skipResponse = await api.post("/api/setup/skip-wizard");
  if (!skipResponse.ok()) {
    throw new Error(`Setup skip failed: ${skipResponse.status()} ${await skipResponse.text()}`);
  }
}

export async function authenticate(
  api: APIRequestContext,
  username = uniqueE2EUsername("e2e_user"),
  password = E2E_PASSWORD,
): Promise<AuthResult> {
  const loginResponse = await api.post("/api/auth/login", {
    data: { username, password },
  });

  if (loginResponse.ok()) {
    const payload = (await loginResponse.json()) as AuthPayload;
    return { token: payload.access_token, userId: payload.user.id, username };
  }

  const registerResponse = await api.post("/api/auth/register", {
    data: { username, password },
  });
  if (!registerResponse.ok()) {
    throw new Error(`Auth setup failed: ${registerResponse.status()} ${await registerResponse.text()}`);
  }
  const payload = (await registerResponse.json()) as AuthPayload;
  return { token: payload.access_token, userId: payload.user.id, username };
}

export async function installAuthToken(page: Page, token: string): Promise<void> {
  await page.addInitScript((value) => {
    window.localStorage.setItem("access_token", value);
  }, token);
}

interface AuthPayload {
  access_token: string;
  user: {
    id: number;
  };
}
