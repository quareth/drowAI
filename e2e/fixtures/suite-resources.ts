/**
 * Allocates marker-guarded filesystem and network resources for one E2E suite.
 *
 * Callers own only the returned temporary child directory; cleanup refuses to
 * remove any existing directory that lacks its matching suite marker.
 */

import { randomUUID } from "node:crypto";
import { cp, mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { sanitizeServerLog } from "./sanitized-logs";

const SUITE_PREFIX = "drowai-e2e-suite-";
const OWNERSHIP_MARKER = ".drowai-e2e-suite.json";
const issuedPorts = new Set<number>();

export interface E2ESuiteResources {
  suiteId: string;
  rootDir: string;
  databasePath: string;
  databaseUrl: string;
  workspaceRoot: string;
  evidenceRoot: string;
  objectStorageRoot: string;
  logRoot: string;
  scenarioMetadataRoot: string;
  ciArtifactRoot?: string;
  generatedConfigRoot: string;
  generatedSecretsRoot: string;
  generatedEnvPath: string;
  backendPort: number;
  frontendPort: number;
  apiBaseUrl: string;
  frontendBaseUrl: string;
  env: Record<string, string>;
}

export interface AllocateSuiteResourcesOptions {
  baseDir?: string;
  label?: string;
  ciArtifactRoot?: string;
}

/** Allocate distinct suite-owned paths and currently free loopback ports. */
export async function allocateSuiteResources(
  options: AllocateSuiteResourcesOptions = {},
): Promise<E2ESuiteResources> {
  const baseDir = resolve(options.baseDir ?? tmpdir());
  await mkdir(baseDir, { recursive: true });

  const label = normalizeLabel(options.label);
  const rootDir = await mkdtemp(join(baseDir, `${SUITE_PREFIX}${label}-`));
  const suiteId = randomUUID();
  const databasePath = join(rootDir, "database.sqlite3");
  const workspaceRoot = join(rootDir, "workspaces");
  const evidenceRoot = join(rootDir, "durable-knowledge");
  const objectStorageRoot = join(rootDir, "object-store");
  const logRoot = join(rootDir, "logs");
  const scenarioMetadataRoot = join(rootDir, "scenario-metadata");
  const generatedConfigRoot = join(rootDir, "generated-config");
  const generatedSecretsRoot = join(rootDir, "generated-secrets");
  const generatedEnvPath = join(rootDir, "generated.env");

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
    join(rootDir, OWNERSHIP_MARKER),
    JSON.stringify({ suiteId }),
    { encoding: "utf8", mode: 0o600 },
  );
  await writeFile(
    join(scenarioMetadataRoot, "suite.json"),
    JSON.stringify(
      {
        suiteId,
        label,
        tier: process.env.E2E_CI_TIER ?? "local",
      },
      null,
      2,
    ),
    { encoding: "utf8", mode: 0o600 },
  );

  const [backendPort, frontendPort] = await allocateFreePorts(2);
  const apiBaseUrl = `http://127.0.0.1:${backendPort}`;
  const frontendBaseUrl = `http://127.0.0.1:${frontendPort}`;
  const databaseUrl = `sqlite:///${databasePath}`;

  return {
    suiteId,
    rootDir,
    databasePath,
    databaseUrl,
    workspaceRoot,
    evidenceRoot,
    objectStorageRoot,
    logRoot,
    scenarioMetadataRoot,
    ciArtifactRoot: resolveOptionalArtifactRoot(
      options.ciArtifactRoot ?? process.env.E2E_CI_ARTIFACT_ROOT,
    ),
    generatedConfigRoot,
    generatedSecretsRoot,
    generatedEnvPath,
    backendPort,
    frontendPort,
    apiBaseUrl,
    frontendBaseUrl,
    env: {
      DATABASE_URL: databaseUrl,
      API_URL: apiBaseUrl,
      BASE_URL: frontendBaseUrl,
      E2E_WORKSPACE_ROOT: workspaceRoot,
      E2E_DURABLE_KNOWLEDGE_ROOT: evidenceRoot,
      DATA_PLANE_LOCAL_OBJECT_STORE_ROOT: objectStorageRoot,
      E2E_LOG_ROOT: logRoot,
      E2E_SCENARIO_METADATA_ROOT: scenarioMetadataRoot,
      E2E_RUNTIME_SUITE_ID: suiteId,
      VITE_E2E_DETERMINISTIC_MODE: "true",
      DROWAI_CONFIG_DIR: generatedConfigRoot,
      DROWAI_SECRETS_DIR: generatedSecretsRoot,
      DROWAI_ENV_FILE: generatedEnvPath,
    },
  };
}

/** Remove only the marker-verified root allocated for this exact suite. */
export async function cleanupSuiteResources(resources: E2ESuiteResources): Promise<void> {
  const markerPath = join(resources.rootDir, OWNERSHIP_MARKER);
  let marker: { suiteId?: string };
  try {
    marker = JSON.parse(await readFile(markerPath, "utf8")) as { suiteId?: string };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      try {
        await stat(resources.rootDir);
      } catch (rootError) {
        if ((rootError as NodeJS.ErrnoException).code === "ENOENT") {
          return;
        }
        throw rootError;
      }
      throw new Error(`Refusing E2E cleanup: ownership marker is missing at ${markerPath}`);
    }
    throw new Error(`Refusing E2E cleanup: ownership marker is invalid at ${markerPath}`);
  }

  if (marker.suiteId !== resources.suiteId) {
    throw new Error(`Refusing E2E cleanup: ownership marker does not match ${resources.suiteId}`);
  }
  let preservationError: unknown;
  try {
    await preserveCiDiagnostics(resources);
  } catch (error) {
    preservationError = error;
  }
  await rm(resources.rootDir, { recursive: true, force: false });
  if (preservationError) {
    throw preservationError;
  }
}

/** Copy only sanitized logs and non-secret scenario metadata before suite cleanup. */
async function preserveCiDiagnostics(resources: E2ESuiteResources): Promise<void> {
  if (!resources.ciArtifactRoot) {
    return;
  }
  const artifactRoot = resolve(resources.ciArtifactRoot);
  const suiteRoot = resolve(resources.rootDir);
  if (artifactRoot === suiteRoot || artifactRoot.startsWith(`${suiteRoot}/`)) {
    throw new Error("E2E CI artifact root must be outside the suite cleanup root");
  }
  const destination = join(artifactRoot, resources.suiteId);
  await mkdir(destination, { recursive: true });
  await Promise.all([
    copySanitizedTextTree(resources.logRoot, join(destination, "logs")),
    copySanitizedTextTree(
      resources.scenarioMetadataRoot,
      join(destination, "scenario-metadata"),
    ),
  ]);
}

async function copySanitizedTextTree(source: string, destination: string): Promise<void> {
  await cp(source, destination, { recursive: true, force: true });
  await sanitizeTextTree(destination);
}

async function sanitizeTextTree(root: string): Promise<void> {
  const entries = await readdir(root, { withFileTypes: true }).catch(() => []);
  await Promise.all(
    entries.map(async (entry) => {
      const path = join(root, entry.name);
      if (entry.isDirectory()) {
        await sanitizeTextTree(path);
        return;
      }
      const contents = await readFile(path, "utf8");
      await writeFile(path, sanitizeServerLog(contents), { encoding: "utf8", mode: 0o600 });
    }),
  );
}

function resolveOptionalArtifactRoot(value: string | undefined): string | undefined {
  const normalized = String(value ?? "").trim();
  return normalized ? resolve(normalized) : undefined;
}

function normalizeLabel(label: string | undefined): string {
  const normalized = String(label ?? "run")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  return normalized || "run";
}

async function allocateFreePorts(count: number): Promise<number[]> {
  const ports: number[] = [];
  while (ports.length < count) {
    const port = await findFreePort();
    if (issuedPorts.has(port)) {
      continue;
    }
    issuedPorts.add(port);
    ports.push(port);
  }
  return ports;
}

function findFreePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("Could not allocate an E2E loopback port"));
        return;
      }
      server.close((error) => {
        if (error) {
          reject(error);
          return;
        }
        resolvePort(address.port);
      });
    });
  });
}
