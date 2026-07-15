/**
 * Helpers to start/stop backend in deterministic E2E mode.
 *
 * These utilities are intentionally process-level only; test suites can call
 * them in global setup/teardown without coupling to Playwright internals.
 */

import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { createConnection } from "node:net";
import { join } from "node:path";

import { captureSanitizedProcessLogs } from "./sanitized-logs";
import {
  allocateSuiteResources,
  cleanupSuiteResources,
  type AllocateSuiteResourcesOptions,
  type E2ESuiteResources,
} from "./suite-resources";

export interface DeterministicBackendHandle {
  process: ChildProcess;
  processes?: ChildProcess[];
  baseUrl: string;
  frontendUrl?: string;
  resources?: E2ESuiteResources;
  ownsResources?: boolean;
}

export interface StartDeterministicBackendOptions {
  command?: string;
  args?: string[];
  cwd?: string;
  baseUrl?: string;
  frontendBaseUrl?: string;
  startupDelayMs?: number;
  extraEnv?: Record<string, string>;
  resources?: E2ESuiteResources;
}

export interface StartDeterministicSuiteOptions
  extends Omit<StartDeterministicBackendOptions, "baseUrl" | "frontendBaseUrl" | "resources"> {
  resources?: AllocateSuiteResourcesOptions;
}

/** Allocate an isolated suite root and own it through stack teardown. */
export async function startDeterministicSuiteStack(
  options: StartDeterministicSuiteOptions = {},
): Promise<DeterministicBackendHandle> {
  const resources = await allocateSuiteResources(options.resources);
  try {
    const handle = await startDeterministicBackend({
      ...options,
      baseUrl: resources.apiBaseUrl,
      frontendBaseUrl: resources.frontendBaseUrl,
      resources,
    });
    return { ...handle, ownsResources: true };
  } catch (error) {
    await cleanupSuiteResources(resources);
    throw error;
  }
}

/**
 * Start the backend with deterministic scenario mode enabled.
 */
export async function startDeterministicBackend(
  options: StartDeterministicBackendOptions = {},
): Promise<DeterministicBackendHandle> {
  const cwd = options.cwd ?? process.cwd();
  const command = options.command ?? defaultPythonCommand(cwd);
  const baseUrl = options.baseUrl ?? options.resources?.apiBaseUrl ?? "http://localhost:8000";
  const backendAddress = resolveBindAddress(baseUrl);
  const args = options.args ?? [
    "-m",
    "uvicorn",
    "backend.main:app",
    "--host",
    backendAddress.host,
    "--port",
    String(backendAddress.port),
    "--lifespan",
    "off",
  ];
  const startupDelayMs = options.startupDelayMs ?? 15_000;
  const frontendUrl =
    options.frontendBaseUrl ??
    options.resources?.frontendBaseUrl ??
    process.env.BASE_URL ??
    "http://localhost:5000";

  await assertUrlPortAvailable(baseUrl, "backend");
  await assertUrlPortAvailable(frontendUrl, "frontend");

  const env = {
    ...process.env,
    DEBUG: process.env.DEBUG ?? "true",
    DROWAI_DEPLOYMENT_PROFILE: "dev_local",
    E2E_DETERMINISTIC_MODE: "true",
    TASK_RUNTIME_PLACEMENT_MODE_DEFAULT: process.env.TASK_RUNTIME_PLACEMENT_MODE_DEFAULT ?? "local",
    VITE_BACKEND_PROXY_TARGET: baseUrl,
    DATABASE_URL: normalizeDatabaseUrl(
      process.env.DATABASE_URL ?? "sqlite:///./.e2e-smoke.sqlite3",
      cwd,
    ),
    ...(options.resources?.env ?? {}),
    ...(options.extraEnv ?? {}),
  };

  await runMigrations(
    command,
    cwd,
    env,
    options.resources ? join(options.resources.logRoot, "migrations.log") : undefined,
  );

  const backendProcess = spawn(command, args, {
    cwd,
    env,
    stdio: options.resources ? ["ignore", "pipe", "pipe"] : "inherit",
    shell: process.platform === "win32",
    detached: process.platform !== "win32",
  });
  const processes = [backendProcess];
  try {
    if (options.resources) {
      await captureSanitizedProcessLogs(
        backendProcess,
        join(options.resources.logRoot, "backend.log"),
      );
    }
    await waitForServiceReady(
      backendProcess,
      `${baseUrl}/api/setup/health`,
      startupDelayMs,
      "backend",
    );

    const frontendAddress = resolveBindAddress(frontendUrl);
    const frontendProcess = spawn(
      "npx",
      ["vite", "--host", frontendAddress.host, "--port", String(frontendAddress.port)],
      {
        cwd,
        env,
        stdio: options.resources ? ["ignore", "pipe", "pipe"] : "inherit",
        shell: process.platform === "win32",
        detached: process.platform !== "win32",
      },
    );
    processes.unshift(frontendProcess);
    if (options.resources) {
      await captureSanitizedProcessLogs(
        frontendProcess,
        join(options.resources.logRoot, "frontend.log"),
      );
    }
    await waitForServiceReady(frontendProcess, frontendUrl, startupDelayMs, "frontend");
    return {
      process: backendProcess,
      processes,
      baseUrl,
      frontendUrl,
      resources: options.resources,
    };
  } catch (error) {
    await terminateProcesses(processes);
    throw error;
  }
}

/**
 * Stop a backend process started by startDeterministicBackend().
 */
export async function stopDeterministicBackend(
  handle: DeterministicBackendHandle | null | undefined,
): Promise<void> {
  if (!handle?.process) {
    return;
  }
  await terminateProcesses(handle.processes ?? [handle.process]);
  if (handle.ownsResources && handle.resources) {
    await cleanupSuiteResources(handle.resources);
  }
}

function defaultPythonCommand(cwd: string): string {
  const venvPython = join(cwd, ".venv", "bin", "python");
  if (existsSync(venvPython)) {
    return venvPython;
  }
  return "python3";
}

function normalizeDatabaseUrl(databaseUrl: string, cwd: string): string {
  const prefix = "sqlite:///";
  if (!databaseUrl.startsWith(prefix)) {
    return databaseUrl;
  }

  const rawPath = databaseUrl.slice(prefix.length);
  if (rawPath.startsWith("/")) {
    return databaseUrl;
  }

  return `${prefix}${join(cwd, rawPath)}`;
}

function resolveBindAddress(rawUrl: string): { host: string; port: number } {
  const parsed = new URL(rawUrl);
  const port = Number(parsed.port || (parsed.protocol === "https:" ? 443 : 80));
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`Invalid service port in URL: ${rawUrl}`);
  }
  return { host: parsed.hostname || "127.0.0.1", port };
}

/** Run schema migrations with the same secret-safe capture used by stack processes. */
export async function runMigrations(
  command: string,
  cwd: string,
  env: NodeJS.ProcessEnv,
  logPath?: string,
): Promise<void> {
  const script = [
    "from pathlib import Path",
    "import os",
    "from backend.config.generated_config import resolved_backend_env",
    "from backend.migrations.runtime import upgrade_database_to_head",
    "env = {**resolved_backend_env(profile='dev_local', docker=False), **os.environ}",
    "upgrade_database_to_head(env=env, repo_root=Path.cwd())",
  ].join("; ");
  const migrationProcess = spawn(command, ["-c", script], {
    cwd,
    env,
    stdio: logPath ? ["ignore", "pipe", "pipe"] : "inherit",
    shell: process.platform === "win32",
  });
  const migrationClose = waitForChildClose(migrationProcess);
  const capture = logPath
    ? await captureSanitizedProcessLogs(migrationProcess, logPath)
    : undefined;
  const exitCode = await migrationClose;
  await capture?.finished;
  if (exitCode !== 0) {
    throw new Error(`Deterministic backend migration failed with exit ${exitCode}`);
  }
}

async function assertUrlPortAvailable(rawUrl: string, label: string): Promise<void> {
  const { host, port } = resolveBindAddress(rawUrl);
  const inUse = await isPortOpen(host, port);
  if (inUse) {
    throw new Error(
      `Cannot start deterministic ${label}: ${host}:${port} is already in use. ` +
        "Stop the existing dev server, or run Playwright with E2E_START_BACKEND=false against an already-running deterministic stack.",
    );
  }
}

function isPortOpen(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = createConnection({ host, port });
    socket.setTimeout(500);
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("timeout", () => {
      socket.destroy();
      resolve(false);
    });
    socket.once("error", () => {
      resolve(false);
    });
  });
}

async function waitForServiceReady(
  processRef: ChildProcess,
  readyUrl: string,
  timeoutMs: number,
  label: string,
): Promise<void> {
  let startupError: Error | null = null;
  processRef.once("error", (error) => {
    startupError = error;
  });

  const deadline = Date.now() + Math.max(timeoutMs, 0);
  while (Date.now() <= deadline) {
    if (startupError) {
      throw startupError;
    }
    if (processRef.exitCode !== null) {
      throw new Error(`Deterministic stack exited before ${label} became ready: ${processRef.exitCode}`);
    }
    try {
      const response = await fetch(readyUrl);
      if (response.ok) {
        return;
      }
    } catch {
      // Backend is still starting.
    }
    await waitForDelay(500);
  }

  throw new Error(`Deterministic ${label} did not become ready within ${timeoutMs}ms`);
}

function waitForDelay(delayMs: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, Math.max(delayMs, 0));
  });
}

async function terminateProcesses(processes: ChildProcess[]): Promise<void> {
  for (const processRef of processes) {
    killProcessTree(processRef, "SIGTERM");
  }
  const stopped = await Promise.all(
    processes.map((processRef) => waitForProcessTreeExit(processRef, 5_000)),
  );
  for (const [index, processRef] of processes.entries()) {
    if (!stopped[index]) {
      killProcessTree(processRef, "SIGKILL");
    }
  }
  await Promise.all(
    processes.map((processRef) => waitForProcessTreeExit(processRef, 5_000)),
  );
  const survivors = processes.filter(isProcessTreeAlive);
  if (survivors.length > 0) {
    throw new Error(`Failed to terminate ${survivors.length} deterministic process tree(s)`);
  }
}

function waitForChildClose(processRef: ChildProcess): Promise<number | null> {
  return new Promise((resolve, reject) => {
    processRef.once("error", reject);
    processRef.once("close", (code) => resolve(code));
    if (processRef.exitCode !== null) {
      resolve(processRef.exitCode);
    }
  });
}

async function waitForProcessTreeExit(
  processRef: ChildProcess,
  timeoutMs: number,
): Promise<boolean> {
  const deadline = Date.now() + Math.max(timeoutMs, 0);
  while (Date.now() <= deadline) {
    if (!isProcessTreeAlive(processRef)) {
      return true;
    }
    await waitForDelay(50);
  }
  return !isProcessTreeAlive(processRef);
}

function isProcessTreeAlive(processRef: ChildProcess): boolean {
  if (process.platform === "win32" || !processRef.pid) {
    return processRef.exitCode === null;
  }
  try {
    process.kill(-processRef.pid, 0);
    return true;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === "ESRCH") {
      return false;
    }
    if (code === "EPERM") {
      return true;
    }
    throw error;
  }
}

function killProcessTree(processRef: ChildProcess, signal: NodeJS.Signals): void {
  if (process.platform !== "win32" && processRef.pid) {
    try {
      process.kill(-processRef.pid, signal);
      return;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") {
        throw error;
      }
    }
  }
  if (process.platform === "win32" && processRef.pid) {
    spawnSync(
      "taskkill",
      ["/pid", String(processRef.pid), "/t", signal === "SIGKILL" ? "/f" : ""].filter(Boolean),
    );
    return;
  }
  processRef.kill(signal);
}
