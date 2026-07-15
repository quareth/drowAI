/** Contract tests for isolated Playwright suite resources and safe cleanup. */

import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  allocateSuiteResources,
  cleanupSuiteResources,
  type E2ESuiteResources,
} from "./suite-resources";

test("allocations own distinct paths, ports, and explicit stack environment", async () => {
  const callerRoot = await mkdtemp(join(tmpdir(), "drowai-e2e-contract-"));
  const sentinelPath = join(callerRoot, "caller-owned.txt");
  await writeFile(sentinelPath, "preserve", "utf8");

  const [first, second] = await Promise.all([
    allocateSuiteResources({ baseDir: callerRoot, label: "first" }),
    allocateSuiteResources({ baseDir: callerRoot, label: "second" }),
  ]);
  try {
    for (const field of [
      "rootDir",
      "databasePath",
      "workspaceRoot",
      "evidenceRoot",
      "objectStorageRoot",
      "logRoot",
      "scenarioMetadataRoot",
    ] as const) {
      assert.notEqual(first[field], second[field], `${field} must be suite-isolated`);
    }
    assert.equal(
      new Set([
        first.backendPort,
        first.frontendPort,
        second.backendPort,
        second.frontendPort,
      ]).size,
      4,
      "concurrent allocations must own four distinct ports",
    );

    assert.equal(first.env.DATABASE_URL, first.databaseUrl);
    assert.equal(first.env.API_URL, first.apiBaseUrl);
    assert.equal(first.env.BASE_URL, first.frontendBaseUrl);
    assert.equal(first.env.E2E_WORKSPACE_ROOT, first.workspaceRoot);
    assert.equal(first.env.E2E_DURABLE_KNOWLEDGE_ROOT, first.evidenceRoot);
    assert.equal(first.env.DATA_PLANE_LOCAL_OBJECT_STORE_ROOT, first.objectStorageRoot);
    assert.equal(first.env.E2E_LOG_ROOT, first.logRoot);
    assert.equal(first.env.E2E_SCENARIO_METADATA_ROOT, first.scenarioMetadataRoot);
    assert.equal(first.env.E2E_RUNTIME_SUITE_ID, first.suiteId);
    assert.equal(first.env.VITE_E2E_DETERMINISTIC_MODE, "true");
    assert.equal(first.env.DROWAI_CONFIG_DIR, first.generatedConfigRoot);
    assert.equal(first.env.DROWAI_SECRETS_DIR, first.generatedSecretsRoot);
    assert.equal(first.env.DROWAI_ENV_FILE, first.generatedEnvPath);
    assert.ok(await stat(first.generatedConfigRoot));
    assert.ok(await stat(first.generatedSecretsRoot));

    await cleanupSuiteResources(first);
    await assert.rejects(stat(first.rootDir));
    assert.equal(await readFile(sentinelPath, "utf8"), "preserve");
    assert.ok(await stat(second.rootDir));
  } finally {
    await cleanupIfPresent(first);
    await cleanupIfPresent(second);
  }
});

test("cleanup rejects a root without the matching ownership marker", async () => {
  const resources = await allocateSuiteResources({ label: "marker-guard" });
  const callerRoot = await mkdtemp(join(tmpdir(), "drowai-e2e-caller-"));
  const forged = { ...resources, rootDir: callerRoot };

  try {
    await assert.rejects(
      cleanupSuiteResources(forged),
      /ownership marker/i,
    );
    assert.ok(await stat(callerRoot));
  } finally {
    await cleanupSuiteResources(resources);
  }
});

test("cleanup preserves only sanitized CI logs and scenario metadata", async () => {
  const callerRoot = await mkdtemp(join(tmpdir(), "drowai-e2e-ci-artifacts-"));
  const artifactRoot = join(callerRoot, "retained");
  const resources = await allocateSuiteResources({
    baseDir: callerRoot,
    label: "failure-contract",
    ciArtifactRoot: artifactRoot,
  });
  const secret = "private-ci-token";
  await writeFile(join(resources.logRoot, "backend.log"), `token=${secret}\n`, "utf8");
  await writeFile(
    join(resources.scenarioMetadataRoot, "scenario.json"),
    JSON.stringify({ password: secret, scenario: "owner-core" }),
    "utf8",
  );

  await cleanupSuiteResources(resources);

  const retainedRoot = join(artifactRoot, resources.suiteId);
  const retainedLog = await readFile(join(retainedRoot, "logs", "backend.log"), "utf8");
  const retainedScenario = await readFile(
    join(retainedRoot, "scenario-metadata", "scenario.json"),
    "utf8",
  );
  const suiteMetadata = JSON.parse(
    await readFile(join(retainedRoot, "scenario-metadata", "suite.json"), "utf8"),
  ) as { suiteId: string; label: string; tier: string };
  assert.equal(retainedLog.includes(secret), false);
  assert.equal(retainedScenario.includes(secret), false);
  assert.deepEqual(suiteMetadata, {
    suiteId: resources.suiteId,
    label: "failure-contract",
    tier: process.env.E2E_CI_TIER ?? "local",
  });
  await assert.rejects(stat(resources.rootDir));
});

async function cleanupIfPresent(resources: E2ESuiteResources): Promise<void> {
  try {
    await cleanupSuiteResources(resources);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw error;
    }
  }
}
