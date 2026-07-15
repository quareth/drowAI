/** Viewer read-only UI and direct-API authorization journey. */

import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";

import {
  actorHeaders,
  createOwnerActor,
  installActorSession,
  type E2EActor,
} from "../fixtures/actors";
import {
  createEngagement,
  createTaskForEngagement,
  type EngagementRecord,
  type TaskRecord,
} from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import {
  seedMembership,
  seedReportingInput,
  seedWorkspaceKnowledge,
} from "../fixtures/offline-seed";

const JOURNEY_TIMEOUT_MS = 150_000;

test("viewer reads owned state but cannot mutate tenant resources", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "viewer-authorization" },
    });
    if (!stack.frontendUrl || !stack.resources) {
      throw new Error("Viewer journey stack did not expose isolated resources.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    const viewerCandidate = await createOwnerActor(api);
    const suffix = Date.now();
    const engagement = await createEngagement(api, viewerCandidate, {
      name: `Viewer engagement ${suffix}`,
    });
    const task = await createTaskForEngagement(api, viewerCandidate, engagement, {
      name: `Viewer task ${suffix}`,
      scope: "e2e://viewer-read-only",
    });
    seedWorkspaceKnowledge({
      resources: stack.resources,
      userId: viewerCandidate.userId,
      tenantId: viewerCandidate.tenantId,
      engagementId: engagement.id,
      taskId: task.id,
      relativePath: `artifacts/viewer-${suffix}.txt`,
      content: `viewer-evidence-${suffix}`,
      findingTitle: `Viewer finding ${suffix}`,
    });
    const reportingInput = seedReportingInput({
      resources: stack.resources,
      userId: viewerCandidate.userId,
      tenantId: viewerCandidate.tenantId,
      engagementId: engagement.id,
      taskId: task.id,
    });
    const membership = seedMembership({
      resources: stack.resources,
      actorUserId: owner.userId,
      targetUserId: viewerCandidate.userId,
      tenantId: owner.tenantId,
      role: "viewer",
    });
    const viewer: E2EActor = {
      ...viewerCandidate,
      role: "viewer",
      membershipId: membership.membership_id,
    };
    await expectViewerContext(api, viewer);

    await installActorSession(page, viewer);
    await verifyReadOnlyUi(page, stack.frontendUrl, viewer, engagement, task);
    await verifyAllowedReads(api, viewer, engagement, task);
    await verifyDirectMutationDenials(api, viewer, engagement, task, reportingInput.memo_id);
    await expectViewerContext(api, viewer);
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

async function verifyReadOnlyUi(
  page: Page,
  frontendUrl: string,
  viewer: E2EActor,
  engagement: EngagementRecord,
  task: TaskRecord,
): Promise<void> {
  await page.goto(frontendUrl, { waitUntil: "domcontentloaded" });
  const engagementToggle = page.getByRole("button", { name: engagement.name, exact: true });
  if ((await engagementToggle.getAttribute("aria-expanded")) !== "true") {
    await engagementToggle.click();
  }
  await expect(page.getByText(task.name, { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "New", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Start", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Shell", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: `Task actions for ${task.name}` }).click();
  await expect(page.getByRole("menuitem", { name: "View Details" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Container Status" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "Delete Task" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  await page.goto(`${frontendUrl}/reports?tab=engagement&engagement_id=${engagement.id}`, {
    waitUntil: "domcontentloaded",
  });
  await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
  await expect(page.getByText(task.name, { exact: true })).toBeVisible();
  await page.getByRole("checkbox", { name: `Select ${task.name}` }).click();
  await expect(page.getByRole("button", { name: "Generate Report" })).toBeDisabled();
  await expect(
    page.getByText("Your current tenant permissions allow report viewing only.").first(),
  ).toBeVisible();

  await page.goto(`${frontendUrl}/settings?section=data-management`, {
    waitUntil: "domcontentloaded",
  });
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByText("Unable to load data management settings")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save policy" })).toBeDisabled();

  await page.goto(`${frontendUrl}/profile?tab=access`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Profile" })).toBeVisible();
  await expect(page.getByText(viewer.username, { exact: true }).first()).toBeVisible();
  await page.getByRole("tab", { name: "Access" }).click();
  await expect(page.getByText("Viewer", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("task.read", { exact: true })).toBeVisible();
}

async function verifyAllowedReads(
  api: APIRequestContext,
  viewer: E2EActor,
  engagement: EngagementRecord,
  task: TaskRecord,
): Promise<void> {
  const taskResponse = await api.get(`/api/tasks/${task.id}`, {
    headers: actorHeaders(viewer),
  });
  expect(taskResponse.ok()).toBe(true);
  expect((await taskResponse.json() as { name: string }).name).toBe(task.name);

  const inputsResponse = await api.get(
    `/api/reporting/engagements/${engagement.id}/inputs`,
    { headers: actorHeaders(viewer) },
  );
  expect(inputsResponse.ok()).toBe(true);
  expect(await inputsResponse.text()).toContain(task.name);
}

async function verifyDirectMutationDenials(
  api: APIRequestContext,
  viewer: E2EActor,
  engagement: EngagementRecord,
  task: TaskRecord,
  memoId: string,
): Promise<void> {
  const headers = actorHeaders(viewer);
  await expectForbidden(await api.post(`/api/tasks/${task.id}/start`, { headers }), "task.control");
  await expectForbidden(await api.delete(`/api/tasks/${task.id}`, { headers }), "task.delete");
  await expectForbidden(
    await api.post(`/api/reporting/engagements/${engagement.id}/reports`, {
      headers,
      data: {
        report_type: "pentest",
        selected_task_memo_ids: [memoId],
        include_candidate_findings: false,
        force_regenerate: false,
      },
    }),
    "report.write",
  );
  await expectForbidden(
    await api.put("/api/settings/data-management", {
      headers,
      data: { report_history_retention_days: 91 },
    }),
    "tenant.settings.manage",
  );
  await expectForbidden(
    await api.get(`/api/tenants/${viewer.tenantId}/memberships`, { headers }),
    "membership",
  );
  await expectForbidden(
    await api.patch(`/api/tenants/${viewer.tenantId}/memberships/${viewer.membershipId}`, {
      headers,
      data: { role: "operator" },
    }),
    "membership",
  );

  const readback = await api.get(`/api/tasks/${task.id}`, { headers });
  expect(readback.ok()).toBe(true);
  expect((await readback.json() as { status: string }).status).toBe("stopped");
}

async function expectViewerContext(api: APIRequestContext, viewer: E2EActor): Promise<void> {
  const response = await api.get("/api/auth/me", { headers: actorHeaders(viewer) });
  expect(response.ok()).toBe(true);
  const payload = await response.json() as {
    active_tenant?: { tenant_id: number; membership_id: number; role: string } | null;
    effective_permissions?: { actions: string[] } | null;
  };
  expect(payload.active_tenant).toMatchObject({
    tenant_id: viewer.tenantId,
    membership_id: viewer.membershipId,
    role: "viewer",
  });
  expect(payload.effective_permissions?.actions).toContain("task.read");
  expect(payload.effective_permissions?.actions).not.toContain("task.control");
}

async function expectForbidden(
  response: { status(): number; text(): Promise<string> },
  detailMarker: string,
): Promise<void> {
  expect(response.status()).toBe(403);
  expect((await response.text()).toLowerCase()).toContain(detailMarker.toLowerCase());
}
