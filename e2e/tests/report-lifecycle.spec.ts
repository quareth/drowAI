/** Deterministic persisted engagement-report generation and deletion lifecycle journey. */

import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";

import { actorHeaders, createOwnerActor, installActorSession, type E2EActor } from "../fixtures/actors";
import { createEngagement, createTaskForEngagement } from "../fixtures/domain-fixtures";
import {
  startDeterministicSuiteStack,
  stopDeterministicBackend,
  type DeterministicBackendHandle,
} from "../fixtures/deterministic-backend";
import { seedReportingInput, seedWorkspaceKnowledge } from "../fixtures/offline-seed";

const JOURNEY_TIMEOUT_MS = 150_000;

test("generates, downloads, histories, deletes, and restores a persisted report", { tag: "@journey" }, async ({ page }) => {
  test.setTimeout(JOURNEY_TIMEOUT_MS);
  let stack: DeterministicBackendHandle | null = null;
  let api: APIRequestContext | null = null;

  try {
    stack = await startDeterministicSuiteStack({
      startupDelayMs: JOURNEY_TIMEOUT_MS,
      resources: { label: "report-lifecycle" },
    });
    if (!stack.frontendUrl || !stack.resources) {
      throw new Error("Report journey stack did not expose isolated resources.");
    }
    api = await request.newContext({ baseURL: stack.baseUrl });
    const owner = await createOwnerActor(api);
    const suffix = Date.now();
    const engagement = await createEngagement(api, owner, { name: `Report engagement ${suffix}` });
    const task = await createTaskForEngagement(api, owner, engagement, {
      name: `Report input ${suffix}`,
      scope: "192.0.2.0/24",
    });
    const evidenceMarker = `report-evidence-${suffix}`;
    seedWorkspaceKnowledge({
      resources: stack.resources,
      userId: owner.userId,
      tenantId: owner.tenantId,
      engagementId: engagement.id,
      taskId: task.id,
      relativePath: `artifacts/report-${suffix}.txt`,
      content: evidenceMarker,
      findingTitle: `Report finding ${suffix}`,
    });
    const reportingInput = seedReportingInput({
      resources: stack.resources,
      userId: owner.userId,
      tenantId: owner.tenantId,
      engagementId: engagement.id,
      taskId: task.id,
    });

    await installActorSession(page, owner);
    await page.goto(
      `${stack.frontendUrl}/reports?tab=engagement&engagement_id=${engagement.id}`,
      { waitUntil: "domcontentloaded" },
    );
    await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
    await expect(page.getByText(task.name, { exact: true })).toBeVisible();
    await page.getByRole("checkbox", { name: `Select ${task.name}` }).click();
    await expect(page.getByText("1 ready input can generate a report.", { exact: true })).toBeVisible();

    const first = await generateReportThroughUi(page, api, owner, engagement.id, "Generate Report");
    expect(first.report.version).toBe(1);
    expect(first.report.source_task_memo_ids).toEqual([reportingInput.memo_id]);
    expect(first.report.markdown_snapshot).toContain("Deterministic E2E executive summary");
    await expect(page.getByRole("heading", { name: "Engagement Report Preview" })).toBeVisible();
    await expect(page.getByText(/Deterministic E2E executive summary/).first()).toBeVisible();

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download report" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("engagement-report-v1.md");

    const second = await generateReportThroughUi(page, api, owner, engagement.id, "Generate New Report");
    expect(second.report.version).toBe(2);
    await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
    await expect(page.getByText("1 previous report.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Open report Engagement Report version 1" }).click();
    await expect(page.getByText("Version 1", { exact: true }).last()).toBeVisible();

    const deleteResponsePromise = page.waitForResponse(
      (response) => response.request().method() === "DELETE" && response.url().endsWith(`/api/reporting/reports/${first.report.id}`),
    );
    await page.getByRole("button", { name: "Delete report", exact: true }).click();
    const deleteResponse = await deleteResponsePromise;
    expect(deleteResponse.ok()).toBe(true);
    expect((await deleteResponse.json() as { report_id: string }).report_id).toBe(first.report.id);
    const hidden = await api.get(`/api/reporting/reports/${first.report.id}`, {
      headers: actorHeaders(owner),
    });
    expect(hidden.status()).toBe(404);

    const undoResponsePromise = page.waitForResponse(
      (response) => response.request().method() === "POST" && response.url().endsWith(`/api/reporting/reports/${first.report.id}/undo-delete`),
    );
    await page.getByRole("button", { name: "Undo", exact: true }).click();
    const undoResponse = await undoResponsePromise;
    expect(undoResponse.ok()).toBe(true);
    expect((await undoResponse.json() as { report_id: string }).report_id).toBe(first.report.id);
    const restored = await api.get(`/api/reporting/reports/${first.report.id}`, {
      headers: actorHeaders(owner),
    });
    expect(restored.ok()).toBe(true);
    expect(await restored.text()).toContain("Deterministic E2E executive summary");
  } finally {
    await api?.dispose();
    await stopDeterministicBackend(stack);
  }
});

async function generateReportThroughUi(
  page: Page,
  api: APIRequestContext,
  owner: E2EActor,
  engagementId: number,
  buttonName: "Generate Report" | "Generate New Report",
): Promise<{ job: ReportJob; report: ReportRead }> {
  const generationResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/reporting/engagements/${engagementId}/reports`),
  );
  await page.getByRole("button", { name: buttonName, exact: true }).click();
  await expect(page.getByRole("heading", { name: "Progress" })).toBeVisible();
  const generationResponse = await generationResponsePromise;
  expect(generationResponse.status()).toBe(202);
  const generation = await generationResponse.json() as { job_id: string; status: string };
  expect(generation.status).toBe("queued");

  const job = await waitForReadyJob(api, owner, generation.job_id);
  expect(job.completed_sections).toHaveLength(job.total_sections);
  expect(job.report_id).toBeTruthy();
  await expect(page.getByText("Report generation is ready.", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  const reportResponse = await api.get(`/api/reporting/reports/${job.report_id}`, {
    headers: actorHeaders(owner),
  });
  expect(reportResponse.ok()).toBe(true);
  return { job, report: await reportResponse.json() as ReportRead };
}

async function waitForReadyJob(
  api: APIRequestContext,
  owner: E2EActor,
  jobId: string,
): Promise<ReportJob> {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const response = await api.get(`/api/reporting/jobs/${jobId}`, {
      headers: actorHeaders(owner),
    });
    if (!response.ok()) {
      throw new Error(`Report job read failed: ${response.status()} ${await response.text()}`);
    }
    const job = await response.json() as ReportJob;
    if (job.status === "ready") {
      return job;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      throw new Error(`Report job ended as ${job.status}: ${job.error_message ?? "no reason"}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("Report job did not become ready within 20 seconds.");
}

interface ReportJob {
  id: string;
  report_id: string | null;
  status: string;
  completed_sections: string[];
  total_sections: number;
  error_message: string | null;
}

interface ReportRead {
  id: string;
  version: number;
  status: string;
  markdown_snapshot: string | null;
  source_task_memo_ids: string[];
}
