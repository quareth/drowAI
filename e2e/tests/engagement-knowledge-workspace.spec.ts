/** Persisted Knowledge workspace journey across real UI, API, and database boundaries. */

import { expect, request, test, type APIRequestContext, type APIResponse, type Page } from "@playwright/test";

import {
  actorHeaders,
  createOwnerActor,
  createViewerActor,
  installActorSession,
  type E2EActor,
} from "../fixtures/actors";
import { createEngagement, createTaskForEngagement } from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import { seedWorkspaceKnowledge, type SeededWorkspaceKnowledge } from "../fixtures/offline-seed";

const JOURNEY_TIMEOUT_MS = 120_000;

test("navigates persisted Knowledge and rejects direct unauthorized reads", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "persisted-knowledge" },
    });
    if (!stack.frontendUrl || !stack.resources) {
      throw new Error("Knowledge journey stack did not expose isolated resources.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    const viewer = await createViewerActor(api, owner, { resources: stack.resources });
    const suffix = Date.now();
    const engagement = await createEngagement(api, owner, { name: `Knowledge territory ${suffix}` });
    const task = await createTaskForEngagement(api, owner, engagement, {
      name: `Knowledge task ${suffix}`,
      scope: "192.0.2.0/24",
    });
    const findingTitle = `Persisted TLS exposure ${suffix}`;
    const evidenceContent = `persisted-evidence-marker-${suffix}`;
    const seeded = seedWorkspaceKnowledge({
      resources: stack.resources,
      userId: owner.userId,
      tenantId: owner.tenantId,
      engagementId: engagement.id,
      taskId: task.id,
      relativePath: `artifacts/knowledge-${suffix}.txt`,
      content: evidenceContent,
      findingTitle,
    });

    await assertPersistedKnowledgeApi(api, owner, seeded, findingTitle);
    await assertUnauthorizedKnowledgeApi(api, viewer, engagement.id, seeded, findingTitle, evidenceContent);

    await installActorSession(page, owner);
    await page.goto(`${stack.frontendUrl}/knowledge`, { waitUntil: "domcontentloaded" });
    await assertKnowledgeTabs(page, engagement.name, seeded, findingTitle, evidenceContent);
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

async function assertPersistedKnowledgeApi(
  api: APIRequestContext,
  owner: E2EActor,
  seeded: SeededWorkspaceKnowledge,
  findingTitle: string,
): Promise<void> {
  const headers = actorHeaders(owner);
  const finding = await api.get(`/api/knowledge/findings/${seeded.finding_id}`, { headers });
  expect(finding.ok()).toBe(true);
  const findingPayload = await finding.json() as {
    title?: string;
    asset?: { id?: string };
    service?: { id?: string };
    evidence_refs?: Array<{ evidence_archive_id?: string }>;
  };
  expect(findingPayload.title).toBe(findingTitle);
  expect(findingPayload.asset?.id).toBe(seeded.asset_id);
  expect(findingPayload.service?.id).toBe(seeded.service_id);
  expect(findingPayload.evidence_refs?.[0]?.evidence_archive_id).toBe(seeded.evidence_id);

  const graph = await api.get("/api/knowledge/relationships/graph", { headers });
  expect(graph.ok()).toBe(true);
  expect(JSON.stringify(await graph.json())).toContain(findingTitle);
}

async function assertUnauthorizedKnowledgeApi(
  api: APIRequestContext,
  viewer: E2EActor,
  engagementId: number,
  seeded: SeededWorkspaceKnowledge,
  findingTitle: string,
  evidenceContent: string,
): Promise<void> {
  const headers = actorHeaders(viewer);
  const attempts: APIResponse[] = [
    await api.get(`/api/knowledge/findings/${seeded.finding_id}`, { headers }),
    await api.get(`/api/knowledge/assets/${seeded.asset_id}`, { headers }),
    await api.post(`/api/knowledge/evidence/${seeded.evidence_id}/read`, {
      headers,
      data: { mode: "head", max_chars: 4000 },
    }),
    await api.get(`/api/engagements/${engagementId}/relationships/graph`, { headers }),
  ];
  for (const response of attempts) {
    expect([403, 404]).toContain(response.status());
    const body = await response.text();
    expect(body).not.toContain(findingTitle);
    expect(body).not.toContain(evidenceContent);
  }
  const viewerFindings = await api.get("/api/knowledge/findings", { headers });
  expect(viewerFindings.ok()).toBe(true);
  expect(await viewerFindings.text()).not.toContain(findingTitle);
}

async function assertKnowledgeTabs(
  page: Page,
  engagementName: string,
  seeded: SeededWorkspaceKnowledge,
  findingTitle: string,
  evidenceContent: string,
): Promise<void> {
  await expect(page).toHaveURL(/\/knowledge$/);
  await expect(page.getByText("Knowledge Overview", { exact: true })).toBeVisible();
  await expect(page.getByText("Open Findings", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Findings", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge\?tab=findings$/);
  await page.getByText(findingTitle, { exact: true }).first().click();
  await expect(page.getByText("Linked Service", { exact: true })).toBeVisible();
  await expect(page.getByText("https-alt", { exact: true })).toBeVisible();
  await expect(page.getByText("Provenance Lineage", { exact: true })).toBeVisible();
  await expect(page.getByText(`Finding Key: ${seeded.finding_key}`, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Preview", exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Evidence Preview" })).toBeVisible();
  await expect(page.getByTestId("engagement-evidence-preview")).toContainText(evidenceContent);
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Assets", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge\?tab=assets$/);
  await page.getByText(/192\.0\.2\./).first().click();
  await expect(page.getByText("Linked Services", { exact: true })).toBeVisible();
  await expect(page.getByText("deterministic-nginx 1.0", { exact: true })).toBeVisible();
  await expect(page.getByText("TCP 8443", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Evidence", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge\?tab=evidence$/);
  await expect(page.getByText("e2e.knowledge_seed", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Preview", exact: true }).first().click();
  await expect(page.getByTestId("engagement-evidence-preview")).toContainText(evidenceContent);
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Territory", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge\?tab=map$/);
  await page.getByRole("combobox", { name: "Engagement" }).selectOption({ label: engagementName });
  await expect(page.getByTestId("territory-topology-canvas")).toBeVisible();
  const inspector = page.getByTestId("territory-asset-inspector");
  await expect(inspector).toContainText(findingTitle);
  await expect(inspector).toContainText("8443");

  await page.getByRole("button", { name: "Briefing", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge\?tab=summary$/);
  await expect(page.getByText("Knowledge Overview", { exact: true })).toBeVisible();
}
