/** Full first-run setup and administrator session lifecycle through the real UI. */

import { expect, request, test, type APIRequestContext } from "@playwright/test";
import { stat } from "node:fs/promises";
import { join } from "node:path";

import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";

const ADMIN_PASSWORD = "e2e-setup-admin-password-123";
const DATABASE_PASSWORD = "e2e-setup-database-password-123";
const STARTUP_TIMEOUT_MS = 90_000;

test("completes setup and preserves a safe administrator session lifecycle", {
  tag: "@journey",
}, async ({ page }) => {
  test.setTimeout(STARTUP_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;
  const adminUsername = `setup_admin_${Date.now()}`;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: STARTUP_TIMEOUT_MS,
      resources: { label: "setup-auth" },
    });
    api = await request.newContext({ baseURL: stack.baseUrl });
    if (!stack.frontendUrl) {
      throw new Error("Isolated setup stack did not expose its frontend URL.");
    }
    const frontendUrl = stack.frontendUrl;

    await page.goto(`${frontendUrl}/settings`, { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.getByRole("heading", { name: "Configure DrowAI" })).toBeVisible();

    await page.getByRole("button", { name: "Start Configuration" }).click();
    await expect(page.getByRole("heading", { name: "Database", exact: true })).toBeVisible();
    await page.getByLabel("Database Password").fill(DATABASE_PASSWORD);
    await page.getByRole("button", { name: "Next" }).click();

    await expect(page.getByRole("heading", { name: "Security", exact: true })).toBeVisible();
    await page.getByLabel("Admin Username").fill(adminUsername);
    await page.getByLabel("Admin Email").fill(`${adminUsername}@example.test`);
    await page.getByLabel("Admin Password").fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Next" }).click();

    await expect(page.getByRole("heading", { name: "Display", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Next" }).click();

    await expect(page.getByRole("heading", { name: "Runner", exact: true })).toBeVisible();
    await page.getByLabel("Runner Site Name").fill("E2E Setup Runner Site");
    await page.getByRole("button", { name: "Next" }).click();

    await expect(page.getByRole("heading", { name: "Review and complete" })).toBeVisible();
    await expect(page.getByText(adminUsername, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Complete Installation" }).click();

    await expect.poll(async () => {
      const setupStatus = await api!.get("/api/setup/status");
      if (!setupStatus.ok()) {
        return `http-${setupStatus.status()}`;
      }
      return ((await setupStatus.json()) as { installation_status?: string }).installation_status;
    }, { timeout: 30_000 }).toBe("complete");
    const setupComplete = page.getByRole("heading", { name: "Setup complete" });
    const authReady = page.getByText("Welcome Back", { exact: true });
    await expect(setupComplete.or(authReady)).toBeVisible({ timeout: 30_000 });
    if (!stack.resources) {
      throw new Error("Isolated setup stack did not expose suite resources.");
    }
    expect((await stat(join(stack.resources.generatedConfigRoot, "backend.env"))).isFile()).toBe(true);
    expect((await stat(join(stack.resources.generatedConfigRoot, "enrollment.toml"))).isFile()).toBe(true);
    expect((await stat(join(stack.resources.generatedSecretsRoot, "jwt_secret"))).isFile()).toBe(true);

    if (await setupComplete.isVisible()) {
      await page.getByRole("button", { name: "Sign in" }).click();
    }
    await expect(page).toHaveURL(/\/auth$/);

    await signIn(page, adminUsername, "invalid-password");
    await expect(page.getByText("Login failed", { exact: true })).toBeVisible();
    await expect(page.getByText("Incorrect username or password", { exact: true })).toBeVisible();
    expect(await readSessionStorage(page)).toEqual({ token: null, tenantId: null });

    await signIn(page, adminUsername, ADMIN_PASSWORD);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByText("Operations").first()).toBeVisible({ timeout: 30_000 });
    await expect.poll(async () => (await readSessionStorage(page)).tenantId).toBe("1");

    await page.getByRole("button", { name: new RegExp(adminUsername) }).click();
    await page.getByRole("menuitem", { name: "Logout" }).click();
    await expect(page).toHaveURL(/\/auth$/);
    await expect(page.getByText("Welcome Back", { exact: true })).toBeVisible({
      timeout: 30_000,
    });
    await expect.poll(async () => readSessionStorage(page)).toEqual({ token: null, tenantId: null });

    await page.goto(`${frontendUrl}/settings`, { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/auth$/);

    await signIn(page, adminUsername, ADMIN_PASSWORD);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByText("Operations").first()).toBeVisible({ timeout: 30_000 });
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

async function signIn(page: import("@playwright/test").Page, username: string, password: string) {
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign In" }).click();
}

async function readSessionStorage(page: import("@playwright/test").Page) {
  return page.evaluate(() => ({
    token: window.localStorage.getItem("access_token"),
    tenantId: window.localStorage.getItem("active_tenant_id"),
  }));
}
