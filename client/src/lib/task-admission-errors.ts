/**
 * User-facing task admission error messages for Runner-only task execution.
 *
 * Responsibilities:
 * - Map structured task admission reason codes to product wording.
 * - Keep create/start surfaces from exposing internal runtime configuration hints.
 */

import { getApiErrorReasonCode, getApiErrorReasonCodes } from "@/lib/response-error";

export interface TaskAdmissionErrorPresentation {
  title: string;
  description: string;
}

const OFFLINE_RUNNER_REASON_CODES = new Set([
  "RUNNER_CREDENTIAL_NOT_ACTIVE",
  "RUNNER_HEARTBEAT_STALE",
  "RUNNER_MAINTENANCE_MODE",
  "RUNNER_NOT_ONLINE",
  "RUNNER_REVOKED",
  "RUNNER_STALE_OR_OFFLINE",
]);

const INCOMPATIBLE_RUNNER_REASON_CODES = new Set([
  "RUNNER_CAPABILITY_MISMATCH",
  "RUNNER_EXECUTION_SITE_MISMATCH",
  "RUNNER_LABEL_MISMATCH",
  "RUNNER_PROTOCOL_INCOMPATIBLE",
  "RUNNER_RUNTIME_VERSION_INCOMPATIBLE",
]);

const RUNTIME_POLICY_REASON_CODES = new Set([
  "PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED",
  "PRODUCT_LOCAL_RUNTIME_REJECTED",
  "PRODUCT_RUNTIME_POLICY_INVALID",
]);

export function taskAdmissionErrorPresentation(
  error: unknown,
  fallbackTitle: string,
): TaskAdmissionErrorPresentation {
  const reasonCode = getApiErrorReasonCode(error);
  const reasonCodes = getApiErrorReasonCodes(error);
  const selectedReasonCode = reasonCode ?? reasonCodes[0] ?? null;

  if (selectedReasonCode === "NO_RUNNERS_REGISTERED") {
    return {
      title: "Runner Site needs a Runner",
      description:
        "No Runner is registered yet. Open Runner Site settings and connect a Runner before creating or starting tasks.",
    };
  }

  if (OFFLINE_RUNNER_REASON_CODES.has(selectedReasonCode ?? "")) {
    return {
      title: "Runner is not connected",
      description:
        "A Runner is registered, but it is not ready for task work. Check Runner Site readiness and reconnect the Runner before trying again.",
    };
  }

  if (INCOMPATIBLE_RUNNER_REASON_CODES.has(selectedReasonCode ?? "")) {
    return {
      title: "Runner is incompatible",
      description:
        "The connected Runner does not match this task's runtime requirements. Update the Runner Site, then try again.",
    };
  }

  if (selectedReasonCode === "RUNNER_CAPACITY_EXHAUSTED") {
    return {
      title: "Runner capacity is exhausted",
      description:
        "Connected Runners are at task capacity. Stop a running task or add another Runner, then try again.",
    };
  }

  if (RUNTIME_POLICY_REASON_CODES.has(selectedReasonCode ?? "")) {
    return {
      title: "Runtime policy needs admin attention",
      description:
        "Task runtime is not configured for Runner execution. Ask an administrator to review setup and Runner Site readiness.",
    };
  }

  return {
    title: fallbackTitle,
    description: error instanceof Error ? error.message : "An unexpected error occurred",
  };
}
