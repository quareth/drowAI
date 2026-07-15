/** Runs guarded offline E2E seeds in a separate Python process. */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { sanitizeServerLog } from "./sanitized-logs";
import type { E2ESuiteResources } from "./suite-resources";

export type TenantMembershipRole = "owner" | "admin" | "operator" | "viewer";

export interface SeedMembershipOptions {
  resources: E2ESuiteResources;
  actorUserId: number;
  targetUserId: number;
  tenantId: number;
  role: TenantMembershipRole;
  cwd?: string;
}

export interface SeededMembership {
  membership_id: number;
  tenant_id: number;
  user_id: number;
  role: TenantMembershipRole;
  status: string;
}

export interface SeedTenantMembershipOptions {
  resources: E2ESuiteResources;
  userId: number;
  tenantSlug: string;
  tenantName: string;
  role?: TenantMembershipRole;
  cwd?: string;
}

export interface SeededTenantMembership extends SeededMembership {
  tenant_slug: string;
  tenant_name: string;
  is_default_tenant: boolean;
}

export interface SeedWorkspaceKnowledgeOptions {
  resources: E2ESuiteResources;
  userId: number;
  tenantId: number;
  engagementId: number;
  taskId: number;
  relativePath: string;
  content: string;
  findingTitle: string;
  cwd?: string;
}

export interface SeededWorkspaceKnowledge {
  task_id: number;
  engagement_id: number;
  relative_path: string;
  finding_key: string;
  finding_title: string;
  finding_upsert_count: number;
  asset_id: string;
  service_id: string;
  finding_id: string;
  evidence_id: string;
}

export interface SeedReportingInputOptions {
  resources: E2ESuiteResources;
  userId: number;
  tenantId: number;
  engagementId: number;
  taskId: number;
  cwd?: string;
}

export interface SeededReportingInput {
  task_id: number;
  engagement_id: number;
  memo_id: string;
  knowledge_ref: string;
  evidence_ref: string;
  source_watermark_hash: string;
}

export interface SeedUsageSettingsOptions {
  resources: E2ESuiteResources;
  userId: number;
  tenantId: number;
  taskId: number;
  conversationId: string;
  cwd?: string;
}

export interface SeededUsageSettings {
  task_id: number;
  record_ids: number[];
  call_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  conversation_id: string;
  credential_masked: boolean;
}

/** Change an existing membership through the backend's membership service. */
export function seedMembership(options: SeedMembershipOptions): SeededMembership {
  const cwd = options.cwd ?? process.cwd();
  const command = resolvePythonCommand(cwd);
  const result = spawnSync(
    command,
    [
      join(cwd, "scripts", "e2e_seed.py"),
      "membership",
      "--actor-user-id",
      String(options.actorUserId),
      "--target-user-id",
      String(options.targetUserId),
      "--tenant-id",
      String(options.tenantId),
      "--role",
      options.role,
    ],
    {
      cwd,
      env: {
        ...process.env,
        ...options.resources.env,
        E2E_DETERMINISTIC_MODE: "true",
      },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  if (result.status !== 0) {
    const diagnostic = sanitizeServerLog(result.stderr || result.stdout || "no diagnostic").trim();
    throw new Error(`Offline membership seed failed with exit ${result.status}: ${diagnostic}`);
  }
  return JSON.parse(result.stdout) as SeededMembership;
}

/** Create one non-default tenant membership behind the guarded offline boundary. */
export function seedTenantMembership(
  options: SeedTenantMembershipOptions,
): SeededTenantMembership {
  const cwd = options.cwd ?? process.cwd();
  const command = resolvePythonCommand(cwd);
  const result = spawnSync(
    command,
    [
      join(cwd, "scripts", "e2e_seed.py"),
      "tenant-membership",
      "--user-id",
      String(options.userId),
      "--tenant-slug",
      options.tenantSlug,
      "--tenant-name",
      options.tenantName,
      "--role",
      options.role ?? "owner",
    ],
    {
      cwd,
      env: {
        ...process.env,
        ...options.resources.env,
        E2E_DETERMINISTIC_MODE: "true",
      },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  if (result.status !== 0) {
    const diagnostic = sanitizeServerLog(result.stderr || result.stdout || "no diagnostic").trim();
    throw new Error(`Offline tenant seed failed with exit ${result.status}: ${diagnostic}`);
  }
  const seeded = JSON.parse(result.stdout) as SeededTenantMembership;
  if (seeded.is_default_tenant) {
    throw new Error("Offline tenant seed unexpectedly returned the default tenant.");
  }
  return seeded;
}

/** Seed task-local text and a deterministic projected finding without an HTTP endpoint. */
export function seedWorkspaceKnowledge(
  options: SeedWorkspaceKnowledgeOptions,
): SeededWorkspaceKnowledge {
  const cwd = options.cwd ?? process.cwd();
  const command = resolvePythonCommand(cwd);
  const result = spawnSync(
    command,
    [
      join(cwd, "scripts", "e2e_seed.py"),
      "workspace-knowledge",
      "--user-id",
      String(options.userId),
      "--tenant-id",
      String(options.tenantId),
      "--engagement-id",
      String(options.engagementId),
      "--task-id",
      String(options.taskId),
      "--relative-path",
      options.relativePath,
      "--content",
      options.content,
      "--finding-title",
      options.findingTitle,
    ],
    {
      cwd,
      env: {
        ...process.env,
        ...options.resources.env,
        E2E_DETERMINISTIC_MODE: "true",
      },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  if (result.status !== 0) {
    const diagnostic = sanitizeServerLog(result.stderr || result.stdout || "no diagnostic").trim();
    throw new Error(`Offline workspace seed failed with exit ${result.status}: ${diagnostic}`);
  }
  return JSON.parse(result.stdout) as SeededWorkspaceKnowledge;
}

/** Seed a stopped task's current ready reporting memo from durable local sources. */
export function seedReportingInput(options: SeedReportingInputOptions): SeededReportingInput {
  const cwd = options.cwd ?? process.cwd();
  const command = resolvePythonCommand(cwd);
  const result = spawnSync(
    command,
    [
      join(cwd, "scripts", "e2e_seed.py"),
      "reporting-input",
      "--user-id",
      String(options.userId),
      "--tenant-id",
      String(options.tenantId),
      "--engagement-id",
      String(options.engagementId),
      "--task-id",
      String(options.taskId),
    ],
    {
      cwd,
      env: {
        ...process.env,
        ...options.resources.env,
        E2E_DETERMINISTIC_MODE: "true",
      },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  if (result.status !== 0) {
    const diagnostic = sanitizeServerLog(result.stderr || result.stdout || "no diagnostic").trim();
    throw new Error(`Offline reporting input seed failed with exit ${result.status}: ${diagnostic}`);
  }
  return JSON.parse(result.stdout) as SeededReportingInput;
}

/** Seed task usage and a suite-owned fake credential without exposing its value. */
export function seedUsageSettings(options: SeedUsageSettingsOptions): SeededUsageSettings {
  const cwd = options.cwd ?? process.cwd();
  const command = resolvePythonCommand(cwd);
  const result = spawnSync(
    command,
    [
      join(cwd, "scripts", "e2e_seed.py"),
      "usage-settings",
      "--user-id",
      String(options.userId),
      "--tenant-id",
      String(options.tenantId),
      "--task-id",
      String(options.taskId),
      "--conversation-id",
      options.conversationId,
    ],
    {
      cwd,
      env: {
        ...process.env,
        ...options.resources.env,
        E2E_DETERMINISTIC_MODE: "true",
      },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  if (result.status !== 0) {
    const diagnostic = sanitizeServerLog(result.stderr || result.stdout || "no diagnostic").trim();
    throw new Error(`Offline usage/settings seed failed with exit ${result.status}: ${diagnostic}`);
  }
  return JSON.parse(result.stdout) as SeededUsageSettings;
}

/** Mirror the guarded seed's deterministic suite-only credential value for leak checks. */
export function usageSettingsCredentialSecret(userId: number, taskId: number): string {
  return `sk-e2e-suite-u${userId}-t${taskId}`;
}

function resolvePythonCommand(cwd: string): string {
  const venvPython = join(cwd, ".venv", "bin", "python");
  return existsSync(venvPython) ? venvPython : "python3";
}
