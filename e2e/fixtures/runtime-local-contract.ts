/**
 * Fail-closed Docker prerequisites and suite-owned cleanup for the real-runtime E2E tier.
 *
 * Diagnostics intentionally expose stable reason codes only. Raw Docker output and
 * absolute host paths never cross this boundary into Playwright artifacts.
 */

import { spawn } from "node:child_process";
import { stat } from "node:fs/promises";
import { relative, resolve, sep } from "node:path";

import {
  cleanupSuiteResources,
  type E2ESuiteResources,
} from "./suite-resources";

const SUITE_LABEL = "drowai.e2e_suite_id";
const RUNTIME_INFO_PATH = "/opt/drowai/runtime/python/executor_daemon.py";
const COMMAND_TIMEOUT_MS = 30_000;

export interface DockerCommandResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  errorCode?: string;
}

export type DockerCommandRunner = (args: string[]) => Promise<DockerCommandResult>;

export interface RuntimeLocalDiagnostic {
  status: "passed" | "failed";
  stage: "platform" | "docker_cli" | "daemon" | "image" | "runtime" | "cleanup" | "complete";
  reason: string;
  image?: string;
  architecture?: "amd64" | "arm64";
}

export interface RuntimeLocalPreflightResult {
  image: string;
  architecture: "amd64" | "arm64";
  diagnostic: RuntimeLocalDiagnostic;
}

export interface RuntimeLocalPreflightOptions {
  resources: E2ESuiteResources;
  platform?: NodeJS.Platform;
  arch?: string;
  image?: string;
  env?: NodeJS.ProcessEnv;
  runDocker?: DockerCommandRunner;
}

export class RuntimeLocalPrerequisiteError extends Error {
  readonly diagnostic: RuntimeLocalDiagnostic;

  constructor(diagnostic: RuntimeLocalDiagnostic) {
    super(`Local runtime prerequisite failed: ${diagnostic.stage}/${diagnostic.reason}`);
    this.name = "RuntimeLocalPrerequisiteError";
    this.diagnostic = diagnostic;
  }
}

/** Fail before filesystem, port, browser, or Docker allocation on unsupported hosts. */
export function assertRuntimeLocalPlatform(platform: NodeJS.Platform = process.platform): void {
  if (platform !== "linux") {
    fail("platform", "linux_required");
  }
}

/** Require Linux, a reachable Docker daemon, the runtime image, and its runtime probe. */
export async function assertRuntimeLocalPrerequisites(
  options: RuntimeLocalPreflightOptions,
): Promise<RuntimeLocalPreflightResult> {
  const platform = options.platform ?? process.platform;
  assertRuntimeLocalPlatform(platform);

  const architecture = normalizeArchitecture(options.arch ?? process.arch);
  const env = options.env ?? process.env;
  const image = String(
    options.image ??
      env.DROWAI_RUNTIME_IMAGE ??
      env.CONTAINER_IMAGE ??
      defaultRuntimeImage(architecture),
  ).trim();
  if (!image) {
    fail("image", "runtime_image_not_configured");
  }
  const runDocker = options.runDocker ?? runDockerCommand;

  const daemonResult = await runDocker(["version", "--format", "{{json .Server}}"]);
  if (daemonResult.errorCode === "ENOENT") {
    fail("docker_cli", "docker_cli_missing");
  }
  if (daemonResult.exitCode !== 0 || !daemonResult.stdout.trim()) {
    fail("daemon", "docker_daemon_unavailable");
  }

  const imageResult = await runDocker([
    "image",
    "inspect",
    "--format",
    "{{json .}}",
    image,
  ]);
  if (imageResult.exitCode !== 0) {
    fail("image", "runtime_image_missing");
  }
  const imageMetadata = parseImageMetadata(imageResult.stdout);
  if (!imageMetadata || imageMetadata.os !== "linux") {
    fail("image", "runtime_image_invalid");
  }
  if (imageMetadata.architecture !== architecture) {
    fail("image", "runtime_image_architecture_mismatch");
  }

  const probeResult = await runDocker([
    "run",
    "--rm",
    "--network",
    "none",
    "--cap-add",
    "NET_ADMIN",
    "--label",
    `${SUITE_LABEL}=${options.resources.suiteId}`,
    "--volume",
    `${options.resources.workspaceRoot}:/workspace:rw`,
    "--entrypoint",
    "python3",
    image,
    RUNTIME_INFO_PATH,
    "--runtime-info",
  ]);
  if (probeResult.exitCode !== 0) {
    fail("runtime", "runtime_capability_probe_failed");
  }
  if (!hasRuntimeManifestContract(probeResult.stdout)) {
    fail("runtime", "runtime_manifest_invalid");
  }

  return {
    image,
    architecture,
    diagnostic: {
      status: "passed",
      stage: "complete",
      reason: "runtime_ready",
      image,
      architecture,
    },
  };
}

/** Track and clean only containers and workspaces carrying this suite's ownership. */
export class RuntimeLocalResourceTracker {
  readonly containerLabel: string;
  private readonly trackedContainers = new Set<string>();
  private readonly trackedWorkspaces = new Set<string>();

  constructor(
    readonly resources: E2ESuiteResources,
    private readonly runDocker: DockerCommandRunner = runDockerCommand,
  ) {
    this.containerLabel = `${SUITE_LABEL}=${resources.suiteId}`;
    this.trackedWorkspaces.add(resolve(resources.workspaceRoot));
  }

  trackContainer(containerName: string): void {
    const normalized = containerName.trim();
    if (!normalized || /[\r\n]/.test(normalized)) {
      throw new Error("Container tracking requires one non-empty container name");
    }
    this.trackedContainers.add(normalized);
  }

  trackWorkspace(workspacePath: string): void {
    const root = resolve(this.resources.workspaceRoot);
    const candidate = resolve(workspacePath);
    const pathFromRoot = relative(root, candidate);
    if (pathFromRoot === ".." || pathFromRoot.startsWith(`..${sep}`)) {
      throw new Error("Refusing to track a workspace outside the suite workspace root");
    }
    this.trackedWorkspaces.add(candidate);
  }

  /** Remove labeled containers and marker-owned files, then fail if either leaked. */
  async cleanupAndAssertNoLeaks(): Promise<void> {
    const failures: string[] = [];
    try {
      const ownedBefore = await this.listOwnedContainers();
      for (const name of ownedBefore) {
        this.trackedContainers.add(name);
      }
      if (ownedBefore.length > 0) {
        const removal = await this.runDocker(["rm", "-f", ...ownedBefore]);
        if (removal.exitCode !== 0) {
          failures.push("suite container cleanup failed");
        }
      }
      const ownedAfter = await this.listOwnedContainers();
      if (ownedAfter.length > 0) {
        failures.push(`container leak (${ownedAfter.length} suite-owned)`);
      }
    } catch (error) {
      failures.push(
        error instanceof RuntimeLocalPrerequisiteError
          ? error.message
          : "suite container cleanup inspection failed",
      );
    }

    try {
      await cleanupSuiteResources(this.resources);
      await assertPathsAbsent(this.resources.rootDir, this.trackedWorkspaces);
    } catch {
      failures.push("suite workspace cleanup or leak assertion failed");
    }

    if (failures.length > 0) {
      throw new Error(`Local runtime cleanup failed: ${failures.join("; ")}`);
    }
  }

  private async listOwnedContainers(): Promise<string[]> {
    const result = await this.runDocker([
      "ps",
      "-aq",
      "--filter",
      `label=${this.containerLabel}`,
    ]);
    if (result.exitCode !== 0) {
      throw new RuntimeLocalPrerequisiteError({
        status: "failed",
        stage: "cleanup",
        reason: "container_inventory_failed",
      });
    }
    return result.stdout
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
  }
}

/** Execute Docker without a shell and cap retained output used for local parsing. */
export function runDockerCommand(args: string[]): Promise<DockerCommandResult> {
  return new Promise((resolveResult) => {
    const child = spawn("docker", args, {
      stdio: ["ignore", "pipe", "pipe"],
      shell: false,
    });
    let stdout = "";
    let stderr = "";
    const append = (current: string, chunk: Buffer | string) =>
      (current + chunk.toString()).slice(-64 * 1024);
    child.stdout?.on("data", (chunk) => {
      stdout = append(stdout, chunk);
    });
    child.stderr?.on("data", (chunk) => {
      stderr = append(stderr, chunk);
    });
    let errorCode: string | undefined;
    child.once("error", (error: NodeJS.ErrnoException) => {
      errorCode = error.code;
    });
    const timeout = setTimeout(() => child.kill("SIGKILL"), COMMAND_TIMEOUT_MS);
    child.once("close", (exitCode) => {
      clearTimeout(timeout);
      resolveResult({ exitCode, stdout, stderr, errorCode });
    });
  });
}

function fail(stage: RuntimeLocalDiagnostic["stage"], reason: string): never {
  throw new RuntimeLocalPrerequisiteError({ status: "failed", stage, reason });
}

function normalizeArchitecture(raw: string): "amd64" | "arm64" {
  const normalized = raw.trim().toLowerCase();
  if (normalized === "x64" || normalized === "x86_64" || normalized === "amd64") {
    return "amd64";
  }
  if (normalized === "arm64" || normalized === "aarch64") {
    return "arm64";
  }
  fail("platform", "unsupported_architecture");
}

function defaultRuntimeImage(architecture: "amd64" | "arm64"): string {
  return `drowai/kali-pentesting:${architecture}-runtime`;
}

function parseImageMetadata(
  value: string,
): { os: string; architecture: "amd64" | "arm64" } | null {
  try {
    const decoded = JSON.parse(value) as unknown;
    const raw = Array.isArray(decoded) ? decoded[0] : decoded;
    if (!raw || typeof raw !== "object") {
      return null;
    }
    const metadata = raw as Record<string, unknown>;
    const os = String(metadata.Os ?? metadata.os ?? "").toLowerCase();
    const architecture = normalizeArchitecture(
      String(metadata.Architecture ?? metadata.architecture ?? ""),
    );
    return { os, architecture };
  } catch {
    return null;
  }
}

function hasRuntimeManifestContract(value: string): boolean {
  try {
    const payload = JSON.parse(value) as Record<string, unknown>;
    return (
      nonEmptyString(payload.runtime_contract_version) &&
      nonEmptyString(payload.file_comm_schema_version) &&
      nonEmptyString(payload.workspace_layout_version) &&
      isNonEmptyRecord(payload.semantic_schema_versions) &&
      Array.isArray(payload.supported_tool_families) &&
      payload.supported_tool_families.some(nonEmptyString)
    );
  } catch {
    return false;
  }
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isNonEmptyRecord(value: unknown): boolean {
  return Boolean(
    value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      Object.keys(value).length > 0,
  );
}

async function assertPathsAbsent(rootDir: string, trackedPaths: Set<string>): Promise<void> {
  for (const path of new Set([resolve(rootDir), ...trackedPaths])) {
    try {
      await stat(path);
      throw new Error("suite-owned path still exists");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        throw error;
      }
    }
  }
}
