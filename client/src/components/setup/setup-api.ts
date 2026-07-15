/**
 * Typed API helpers for the standalone setup wizard.
 */
import { apiRequest } from "@/lib/queryClient";
import type {
  SetupCompleteResponse,
  SetupConfig,
  SetupStatus,
} from "@/components/setup/setup-types";

export async function fetchSetupStatus(): Promise<SetupStatus> {
  return parseSetupStatus(await apiRequest("/api/setup/status"));
}

export async function completeSetup(config: SetupConfig): Promise<SetupCompleteResponse> {
  return parseSetupCompleteResponse(
    await apiRequest("/api/setup/complete", {
      method: "POST",
      body: JSON.stringify(config),
    }),
  );
}

export async function skipSetupWizard(): Promise<SetupCompleteResponse> {
  return parseSetupCompleteResponse(
    await apiRequest("/api/setup/skip-wizard", {
      method: "POST",
    }),
  );
}

function parseSetupStatus(value: unknown): SetupStatus {
  const record = requireRecord(value, "setup status");
  return {
    setup_required: requireBoolean(record.setup_required, "setup_required"),
    wizard_enabled: requireBoolean(record.wizard_enabled, "wizard_enabled"),
    installation_complete: requireBoolean(record.installation_complete, "installation_complete"),
    installation_status: requireInstallationStatus(record.installation_status),
    setup_error: optionalNullableString(record.setup_error, "setup_error"),
    deployment_profile: requireString(record.deployment_profile, "deployment_profile"),
    database_accessible: requireBoolean(record.database_accessible, "database_accessible"),
    runner_connected: requireBoolean(record.runner_connected, "runner_connected"),
  };
}

function parseSetupCompleteResponse(value: unknown): SetupCompleteResponse {
  const record = requireRecord(value, "setup completion response");
  return {
    status: requireString(record.status, "status"),
    message: requireString(record.message, "message"),
    redirect: optionalString(record.redirect, "redirect"),
    admin_username: requireString(record.admin_username, "admin_username"),
    runner_site_created: requireBoolean(record.runner_site_created, "runner_site_created"),
    runner_enrollment_published: requireBoolean(
      record.runner_enrollment_published,
      "runner_enrollment_published",
    ),
    runner_readiness: requireRunnerReadiness(record.runner_readiness),
    runtime_services_started: optionalBoolean(record.runtime_services_started, "runtime_services_started"),
    restart_required: optionalBoolean(record.restart_required, "restart_required"),
  };
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`Invalid ${label} payload`);
  }
  return value as Record<string, unknown>;
}

function requireBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`Invalid setup payload field: ${field}`);
  }
  return value;
}

function requireString(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new Error(`Invalid setup payload field: ${field}`);
  }
  return value;
}

function requireInstallationStatus(value: unknown): SetupStatus["installation_status"] {
  const status = requireString(value, "installation_status");
  if (
    status !== "pending" &&
    status !== "provisioning" &&
    status !== "complete" &&
    status !== "failed"
  ) {
    throw new Error("Invalid setup payload field: installation_status");
  }
  return status;
}

function requireRunnerReadiness(value: unknown): SetupCompleteResponse["runner_readiness"] {
  const runnerReadiness = requireString(value, "runner_readiness");
  if (runnerReadiness !== "ready" && runnerReadiness !== "waiting_for_runner") {
    throw new Error("Invalid setup payload field: runner_readiness");
  }
  return runnerReadiness;
}

function optionalString(value: unknown, field: string): string | undefined {
  if (value === undefined) {
    return undefined;
  }
  return requireString(value, field);
}

function optionalBoolean(value: unknown, field: string): boolean | undefined {
  if (value === undefined) {
    return undefined;
  }
  return requireBoolean(value, field);
}

function optionalNullableString(value: unknown, field: string): string | null {
  if (value === undefined || value === null) {
    return null;
  }
  return requireString(value, field);
}
