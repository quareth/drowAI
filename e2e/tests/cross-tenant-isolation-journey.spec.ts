/** Cross-tenant non-disclosure and browser tenant-cache clearing journey. */

import {
  expect,
  request,
  test,
  type APIRequestContext,
  type APIResponse,
  type Page,
} from "@playwright/test";

import {
  actorHeaders,
  createOwnerActor,
  installActorSession,
  type E2EActor,
} from "../fixtures/actors";
import { createEngagement, createTaskForEngagement } from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import {
  seedReportingInput,
  seedTenantMembership,
  seedWorkspaceKnowledge,
  type SeededTenantMembership,
} from "../fixtures/offline-seed";

const JOURNEY_TIMEOUT_MS = 180_000;

test("does not disclose another tenant's persisted or streamed resources", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "cross-tenant-nondisclosure" },
    });
    if (!stack.frontendUrl || !stack.resources) {
      throw new Error("Cross-tenant journey stack did not expose isolated resources.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const protectedBase = await createOwnerActor(api);
    const intruderBase = await createOwnerActor(api);
    const suffix = Date.now();
    const protectedActor = actorForTenant(
      protectedBase,
      seedTenantMembership({
        resources: stack.resources,
        userId: protectedBase.userId,
        tenantSlug: `e2e-protected-${suffix}`,
        tenantName: `Protected tenant ${suffix}`,
      }),
    );
    const intruder = actorForTenant(
      intruderBase,
      seedTenantMembership({
        resources: stack.resources,
        userId: intruderBase.userId,
        tenantSlug: `e2e-intruder-${suffix}`,
        tenantName: `Intruder tenant ${suffix}`,
      }),
    );
    const protectedName = `protected-task-${suffix}`;
    const conversationMarker = `protected-conversation-${suffix}`;
    const filename = `protected-workspace-${suffix}.txt`;
    const fileContent = `protected-file-content-${suffix}`;
    const findingTitle = `protected-finding-${suffix}`;
    const engagement = await createEngagement(api, protectedActor, {
      name: `Protected engagement ${suffix}`,
    });
    const protectedTask = await createTaskForEngagement(api, protectedActor, engagement, {
      name: protectedName,
      scope: `e2e://protected-${suffix}`,
    });
    const knowledge = seedWorkspaceKnowledge({
      resources: stack.resources,
      userId: protectedActor.userId,
      tenantId: protectedActor.tenantId,
      engagementId: engagement.id,
      taskId: protectedTask.id,
      relativePath: `artifacts/${filename}`,
      content: fileContent,
      findingTitle,
    });
    await createDeterministicConversation(api, protectedActor, protectedTask.id, conversationMarker);
    const reportingInput = seedReportingInput({
      resources: stack.resources,
      userId: protectedActor.userId,
      tenantId: protectedActor.tenantId,
      engagementId: engagement.id,
      taskId: protectedTask.id,
    });
    const reportId = await generateDeterministicReport(
      api,
      protectedActor,
      engagement.id,
      reportingInput.memo_id,
    );

    const protectedMarkers = [
      protectedName,
      conversationMarker,
      filename,
      fileContent,
      findingTitle,
      reportId,
    ];
    await assertTenantListsDoNotDisclose(api, intruder, protectedTask.id, protectedMarkers);
    await assertDirectReadsDoNotDisclose(
      api,
      intruder,
      protectedTask.id,
      filename,
      knowledge.finding_id,
      knowledge.evidence_id,
      reportId,
      protectedMarkers,
    );

    await installActorSession(page, intruder);
    await page.goto(stack.frontendUrl, { waitUntil: "domcontentloaded" });
    const streamMessages = await subscribeToForeignTaskStream(
      page,
      stack.baseUrl,
      intruder,
      protectedTask.id,
    );
    expect(streamMessages).toContainEqual(
      expect.objectContaining({ type: "error", message: "forbidden_task" }),
    );
    expect(JSON.stringify(streamMessages)).not.toContain(conversationMarker);
    expect(JSON.stringify(streamMessages)).not.toContain(fileContent);

  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

test("tenant switches and logout clear tenant-owned UI state", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "tenant-cache-clearing" },
    });
    if (!stack.frontendUrl || !stack.resources) {
      throw new Error("Tenant-cache journey stack did not expose isolated resources.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const baseActor = await createOwnerActor(api);
    const suffix = Date.now();
    const tenantASeed = seedTenantMembership({
      resources: stack.resources,
      userId: baseActor.userId,
      tenantSlug: `e2e-cache-a-${suffix}`,
      tenantName: `Cache tenant A ${suffix}`,
    });
    const tenantBSeed = seedTenantMembership({
      resources: stack.resources,
      userId: baseActor.userId,
      tenantSlug: `e2e-cache-b-${suffix}`,
      tenantName: `Cache tenant B ${suffix}`,
    });
    const actorA = actorForTenant(baseActor, tenantASeed);
    const actorB = actorForTenant(baseActor, tenantBSeed);
    const engagementA = await createEngagement(api, actorA, { name: `Cache engagement A ${suffix}` });
    const engagementB = await createEngagement(api, actorB, { name: `Cache engagement B ${suffix}` });
    const taskA = await createTaskForEngagement(api, actorA, engagementA, {
      name: `Cache-only task A ${suffix}`,
    });
    const taskB = await createTaskForEngagement(api, actorB, engagementB, {
      name: `Cache-only task B ${suffix}`,
    });

    await installActorSession(page, actorA);
    await page.goto(stack.frontendUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByText(taskA.name, { exact: true }).first()).toBeVisible();
    await expect(page.getByText(taskB.name, { exact: true })).toHaveCount(0);

    await switchTenantThroughUi(page, tenantBSeed.tenant_name);
    await expect(page.getByText(taskB.name, { exact: true }).first()).toBeVisible();
    await expect(page.getByText(taskA.name, { exact: true })).toHaveCount(0);

    await switchTenantThroughUi(page, tenantASeed.tenant_name);
    await expect(page.getByText(taskA.name, { exact: true }).first()).toBeVisible();
    await expect(page.getByText(taskB.name, { exact: true })).toHaveCount(0);

    await page.getByRole("button", { name: new RegExp(baseActor.username) }).click();
    await page.getByRole("menuitem", { name: "Logout" }).click();
    await expect(page).toHaveURL(/\/auth$/);
    await expect(page.locator("body")).not.toContainText(taskA.name);
    await expect(page.locator("body")).not.toContainText(taskB.name);
    await expect
      .poll(() => page.evaluate(() => ({
        token: window.localStorage.getItem("access_token"),
        tenant: window.localStorage.getItem("active_tenant_id"),
      })))
      .toEqual({ token: null, tenant: null });
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

function actorForTenant(base: E2EActor, seeded: SeededTenantMembership): E2EActor {
  return {
    ...base,
    role: seeded.role === "viewer" ? "viewer" : "owner",
    tenantId: seeded.tenant_id,
    membershipId: seeded.membership_id,
  };
}

async function createDeterministicConversation(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
  marker: string,
): Promise<void> {
  const response = await api.post(`/api/tasks/${taskId}/chat`, {
    headers: actorHeaders(actor),
    data: { message: marker },
  });
  expect(response.status()).toBe(202);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const history = await api.get(`/api/tasks/${taskId}/chat/history?limit=200`, {
      headers: actorHeaders(actor),
    });
    if (history.ok() && (await history.text()).includes(marker)) {
      return;
    }
    await wait(250);
  }
  throw new Error("Protected deterministic conversation did not persist.");
}

async function generateDeterministicReport(
  api: APIRequestContext,
  actor: E2EActor,
  engagementId: number,
  memoId: string,
): Promise<string> {
  const response = await api.post(`/api/reporting/engagements/${engagementId}/reports`, {
    headers: actorHeaders(actor),
    data: {
      report_type: "pentest",
      selected_task_memo_ids: [memoId],
      include_candidate_findings: false,
      force_regenerate: false,
    },
  });
  expect(response.status()).toBe(202);
  const accepted = await response.json() as { job_id: string };
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const jobResponse = await api.get(`/api/reporting/jobs/${accepted.job_id}`, {
      headers: actorHeaders(actor),
    });
    expect(jobResponse.ok()).toBe(true);
    const job = await jobResponse.json() as { status: string; report_id?: string | null };
    if (job.status === "ready" && job.report_id) {
      return job.report_id;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      throw new Error(`Protected report generation ended as ${job.status}.`);
    }
    await wait(250);
  }
  throw new Error("Protected report did not become ready.");
}

async function assertTenantListsDoNotDisclose(
  api: APIRequestContext,
  actor: E2EActor,
  protectedTaskId: number,
  markers: string[],
): Promise<void> {
  const headers = actorHeaders(actor);
  const responses = [
    await api.get("/api/tasks/", { headers }),
    await api.get("/api/knowledge/findings", { headers }),
    await api.get("/api/knowledge/evidence", { headers }),
    await api.get("/api/reporting/reports", { headers }),
    await api.get(`/api/interactive-runs/statuses?task_ids=${protectedTaskId}`, { headers }),
  ];
  for (const response of responses) {
    expect(response.ok()).toBe(true);
    assertBodyOmits(await response.text(), markers);
  }
}

async function assertDirectReadsDoNotDisclose(
  api: APIRequestContext,
  actor: E2EActor,
  taskId: number,
  filename: string,
  findingId: string,
  evidenceId: string,
  reportId: string,
  markers: string[],
): Promise<void> {
  const headers = actorHeaders(actor);
  const responses: APIResponse[] = [
    await api.get(`/api/tasks/${taskId}`, { headers }),
    await api.get(`/api/tasks/${taskId}/chat/history?limit=200`, { headers }),
    await api.get(`/api/tasks/${taskId}/files/tree`, { headers }),
    await api.get(`/api/tasks/${taskId}/files/content`, {
      headers,
      params: { path: `artifacts/${filename}` },
    }),
    await api.get(`/api/tasks/${taskId}/streaming-status`, { headers }),
    await api.get(`/api/knowledge/findings/${findingId}`, { headers }),
    await api.post(`/api/knowledge/evidence/${evidenceId}/read`, {
      headers,
      data: { mode: "head", max_chars: 4000 },
    }),
    await api.get(`/api/reporting/reports/${reportId}`, { headers }),
  ];
  for (const response of responses) {
    expect([403, 404]).toContain(response.status());
    assertBodyOmits(await response.text(), markers);
  }
}

async function subscribeToForeignTaskStream(
  page: Page,
  apiBaseUrl: string,
  actor: E2EActor,
  taskId: number,
): Promise<Array<Record<string, unknown>>> {
  const wsUrl = apiBaseUrl.replace(/^http/, "ws") + "/ws?type=agent-multi";
  return page.evaluate(
    ({ url, token, tenantId, foreignTaskId }) => new Promise<Array<Record<string, unknown>>>((resolve, reject) => {
      const messages: Array<Record<string, unknown>> = [];
      const socket = new WebSocket(url, [`Bearer.${token}`, `Tenant.${tenantId}`]);
      const timeout = window.setTimeout(() => {
        socket.close();
        reject(new Error("Foreign runtime stream did not reject within five seconds."));
      }, 5_000);
      socket.onmessage = (event) => {
        const payload = JSON.parse(String(event.data)) as Record<string, unknown>;
        messages.push(payload);
        if (payload.type === "connection_accepted") {
          socket.send(JSON.stringify({ action: "subscribe", channel: "agent", taskId: foreignTaskId }));
        }
        if (payload.type === "error") {
          window.clearTimeout(timeout);
          socket.close();
          resolve(messages);
        }
      };
      socket.onerror = () => {
        window.clearTimeout(timeout);
        reject(new Error("Foreign runtime stream failed before returning a denial."));
      };
    }),
    { url: wsUrl, token: actor.token, tenantId: actor.tenantId, foreignTaskId: taskId },
  );
}

async function switchTenantThroughUi(page: Page, tenantName: string): Promise<void> {
  const selector = page.getByRole("combobox", { name: "Tenant" });
  await selector.click();
  await page.getByRole("option", { name: tenantName, exact: true }).click();
  await expect(selector).toContainText(tenantName);
}

function assertBodyOmits(body: string, markers: string[]): void {
  for (const marker of markers) {
    expect(body).not.toContain(marker);
  }
}

function wait(delayMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}
