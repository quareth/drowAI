/** Contract tests for fail-closed local-Docker E2E prerequisites and cleanup. */

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdir, mkdtemp, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  RuntimeLocalPrerequisiteError,
  RuntimeLocalResourceTracker,
  assertRuntimeLocalPlatform,
  assertRuntimeLocalPrerequisites,
  type DockerCommandResult,
} from "./runtime-local-contract";
import { cleanupSuiteResources, type E2ESuiteResources } from "./suite-resources";

const runtimeManifest = JSON.stringify({
  runtime_contract_version: "1.1",
  file_comm_schema_version: "2.0",
  workspace_layout_version: "1.0",
  semantic_schema_versions: { network: "1.0", web: "1.0" },
  supported_tool_families: ["filesystem"],
});

test("preflight fails explicitly before Docker on non-Linux hosts", async () => {
  const resources = await makeTestResources("runtime-platform");
  let commandCalls = 0;
  try {
    await assert.rejects(
      assertRuntimeLocalPrerequisites({
        resources,
        platform: "darwin",
        runDocker: async () => {
          commandCalls += 1;
          return ok("");
        },
      }),
      (error: unknown) => {
        assert.ok(error instanceof RuntimeLocalPrerequisiteError);
        assert.deepEqual(error.diagnostic, {
          status: "failed",
          stage: "platform",
          reason: "linux_required",
        });
        assert.equal(error.message.includes(resources.rootDir), false);
        return true;
      },
    );
    assert.equal(commandCalls, 0);
  } finally {
    await cleanupSuiteResources(resources);
  }
});

test("platform gate fails before suite resources or browser startup are needed", () => {
  assert.throws(
    () => assertRuntimeLocalPlatform("darwin"),
    (error: unknown) =>
      error instanceof RuntimeLocalPrerequisiteError &&
      error.diagnostic.stage === "platform" &&
      error.diagnostic.reason === "linux_required",
  );
  assert.doesNotThrow(() => assertRuntimeLocalPlatform("linux"));
});

test("preflight distinguishes missing CLI, daemon, image, and runtime capability failures", async () => {
  const resources = await makeTestResources("runtime-failures");
  const cases: Array<{
    expected: string;
    results: DockerCommandResult[];
  }> = [
    { expected: "docker_cli_missing", results: [{ exitCode: null, stdout: "", stderr: "private", errorCode: "ENOENT" }] },
    { expected: "docker_daemon_unavailable", results: [failed("private daemon socket path")] },
    { expected: "runtime_image_missing", results: [ok("{}"), failed("private registry credential")] },
    { expected: "runtime_capability_probe_failed", results: [ok("{}"), ok('[{"Id":"sha256:abc","Os":"linux","Architecture":"amd64"}]'), failed("secret probe output")] },
  ];
  try {
    for (const item of cases) {
      let index = 0;
      await assert.rejects(
        assertRuntimeLocalPrerequisites({
          resources,
          platform: "linux",
          arch: "x64",
          image: "example.invalid/runtime:local",
          runDocker: async () => item.results[index++] ?? ok(runtimeManifest),
        }),
        (error: unknown) => {
          assert.ok(error instanceof RuntimeLocalPrerequisiteError);
          assert.equal(error.diagnostic.reason, item.expected);
          assert.equal(error.message.includes("private"), false);
          assert.equal(error.message.includes("secret"), false);
          assert.equal(error.message.includes(resources.rootDir), false);
          return true;
        },
      );
    }
  } finally {
    await cleanupSuiteResources(resources);
  }
});

test("preflight validates image architecture and the runtime manifest without leaking raw output", async () => {
  const resources = await makeTestResources("runtime-contract");
  try {
    await assert.rejects(
      assertRuntimeLocalPrerequisites({
        resources,
        platform: "linux",
        arch: "x64",
        image: "runtime:test",
        runDocker: sequence([
          ok("{}"),
          ok('[{"Id":"sha256:abc","Os":"linux","Architecture":"arm64"}]'),
        ]),
      }),
      (error: unknown) =>
        error instanceof RuntimeLocalPrerequisiteError &&
        error.diagnostic.reason === "runtime_image_architecture_mismatch",
    );

    await assert.rejects(
      assertRuntimeLocalPrerequisites({
        resources,
        platform: "linux",
        arch: "x64",
        image: "runtime:test",
        runDocker: sequence([
          ok("{}"),
          ok('[{"Id":"sha256:abc","Os":"linux","Architecture":"amd64"}]'),
          ok('{"runtime_contract_version":"private-incomplete-payload"}'),
        ]),
      }),
      (error: unknown) =>
        error instanceof RuntimeLocalPrerequisiteError &&
        error.diagnostic.reason === "runtime_manifest_invalid",
    );
  } finally {
    await cleanupSuiteResources(resources);
  }
});

test("preflight labels its capability probe and returns sanitized diagnostics", async () => {
  const resources = await makeTestResources("runtime-pass");
  const calls: string[][] = [];
  try {
    const result = await assertRuntimeLocalPrerequisites({
      resources,
      platform: "linux",
      arch: "x64",
      image: "runtime:test",
      runDocker: async (args) => {
        calls.push(args);
        return [ok("{}"), ok('[{"Id":"sha256:abc","Os":"linux","Architecture":"amd64"}]'), ok(runtimeManifest)][calls.length - 1];
      },
    });

    assert.equal(result.image, "runtime:test");
    assert.equal(result.architecture, "amd64");
    assert.deepEqual(result.diagnostic, {
      status: "passed",
      stage: "complete",
      reason: "runtime_ready",
      image: "runtime:test",
      architecture: "amd64",
    });
    assert.ok(calls[2].includes(`drowai.e2e_suite_id=${resources.suiteId}`));
    assert.ok(calls[2].includes("NET_ADMIN"));
    assert.ok(calls[2].includes(`${resources.workspaceRoot}:/workspace:rw`));
  } finally {
    await cleanupSuiteResources(resources);
  }
});

test("tracker removes only suite-labeled containers and asserts container and workspace cleanup", async () => {
  const resources = await makeTestResources("runtime-cleanup");
  const taskWorkspace = join(resources.workspaceRoot, "task-41");
  await mkdir(taskWorkspace, { recursive: true });
  await writeFile(join(taskWorkspace, "owned.txt"), "owned", "utf8");

  const calls: string[][] = [];
  const results = [ok("container-a\ncontainer-b\n"), ok(""), ok("")];
  const tracker = new RuntimeLocalResourceTracker(resources, async (args) => {
    calls.push(args);
    return results[calls.length - 1] ?? ok("");
  });
  tracker.trackContainer("container-a");
  tracker.trackWorkspace(taskWorkspace);

  await tracker.cleanupAndAssertNoLeaks();

  assert.deepEqual(calls[0], [
    "ps", "-aq", "--filter", `label=drowai.e2e_suite_id=${resources.suiteId}`,
  ]);
  assert.deepEqual(calls[1], ["rm", "-f", "container-a", "container-b"]);
  assert.deepEqual(calls[2], calls[0]);
  await assert.rejects(stat(resources.rootDir));
});

test("tracker reports a post-cleanup Docker leak and rejects out-of-suite workspaces", async () => {
  const resources = await makeTestResources("runtime-leak");
  const tracker = new RuntimeLocalResourceTracker(
    resources,
    sequence([ok("container-a\n"), ok(""), ok("container-a\n")]),
  );
  try {
    assert.throws(
      () => tracker.trackWorkspace(join(resources.rootDir, "..", "caller-owned")),
      /outside the suite workspace root/i,
    );
    await assert.rejects(tracker.cleanupAndAssertNoLeaks(), /container leak/i);
    await assert.rejects(stat(resources.rootDir));
  } finally {
    try {
      await cleanupSuiteResources(resources);
    } catch {
      // cleanupAndAssertNoLeaks already removes the suite root.
    }
  }
});

function ok(stdout: string): DockerCommandResult {
  return { exitCode: 0, stdout, stderr: "" };
}

function failed(stderr: string): DockerCommandResult {
  return { exitCode: 1, stdout: "", stderr };
}

function sequence(results: DockerCommandResult[]) {
  let index = 0;
  return async (): Promise<DockerCommandResult> => results[index++] ?? ok("");
}

async function makeTestResources(label: string): Promise<E2ESuiteResources> {
  const rootDir = await mkdtemp(join(tmpdir(), `drowai-e2e-suite-${label}-`));
  const suiteId = randomUUID();
  const workspaceRoot = join(rootDir, "workspaces");
  const evidenceRoot = join(rootDir, "durable-knowledge");
  const objectStorageRoot = join(rootDir, "object-store");
  const logRoot = join(rootDir, "logs");
  const scenarioMetadataRoot = join(rootDir, "scenario-metadata");
  const generatedConfigRoot = join(rootDir, "generated-config");
  const generatedSecretsRoot = join(rootDir, "generated-secrets");
  await Promise.all(
    [
      workspaceRoot,
      evidenceRoot,
      objectStorageRoot,
      logRoot,
      scenarioMetadataRoot,
      generatedConfigRoot,
      generatedSecretsRoot,
    ].map((path) => mkdir(path, { recursive: true })),
  );
  await writeFile(
    join(rootDir, ".drowai-e2e-suite.json"),
    JSON.stringify({ suiteId }),
    { encoding: "utf8", mode: 0o600 },
  );
  return {
    suiteId,
    rootDir,
    databasePath: join(rootDir, "database.sqlite3"),
    databaseUrl: `sqlite:///${join(rootDir, "database.sqlite3")}`,
    workspaceRoot,
    evidenceRoot,
    objectStorageRoot,
    logRoot,
    scenarioMetadataRoot,
    generatedConfigRoot,
    generatedSecretsRoot,
    generatedEnvPath: join(rootDir, "generated.env"),
    backendPort: 18080,
    frontendPort: 15173,
    apiBaseUrl: "http://127.0.0.1:18080",
    frontendBaseUrl: "http://127.0.0.1:15173",
    env: { E2E_RUNTIME_SUITE_ID: suiteId },
  };
}
