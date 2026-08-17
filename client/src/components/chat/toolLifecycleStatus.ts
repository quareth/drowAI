/**
 * Canonical lifecycle precedence and labels for tool execution cards.
 *
 * This module keeps process, session, and generic invocation status
 * interpretation consistent across single-tool and batch presentations.
 */

export type ToolLifecycleStatus =
  | "executing"
  | "completed"
  | "failed"
  | "cancelled"
  | "terminated"
  | "timed_out";

interface ProcessPresentation {
  toolStatus: ToolLifecycleStatus;
  processLabel: string;
}

const PROCESS_PRESENTATION: Record<string, ProcessPresentation> = {
  running: { toolStatus: "executing", processLabel: "" },
  completed: { toolStatus: "completed", processLabel: "Process completed" },
  timed_out: { toolStatus: "timed_out", processLabel: "Process timed out" },
  terminated: { toolStatus: "terminated", processLabel: "Process terminated" },
  failed: { toolStatus: "failed", processLabel: "Process failed" },
};

function normalize(value: string | undefined): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function deriveGenericToolStatus(rawStatus: string | undefined): ToolLifecycleStatus {
  const status = normalize(rawStatus);
  if (status === "success" || status === "ok" || status === "completed") {
    return "completed";
  }
  if (status === "running" || status === "in_progress") return "executing";
  if (status === "timed_out" || status === "timeout") return "timed_out";
  if (status === "terminated") return "terminated";
  if (
    status === "cancelled" ||
    status === "canceled" ||
    status === "cancel_requested" ||
    status === "stopped"
  ) {
    return "cancelled";
  }
  return "failed";
}

export function deriveToolLifecycleStatus(
  rawStatus: string | undefined,
  processStatus: string | undefined,
): ToolLifecycleStatus {
  const process = PROCESS_PRESENTATION[normalize(processStatus)];
  return process?.toolStatus ?? deriveGenericToolStatus(rawStatus);
}

interface ShellLifecycleValues {
  sessionStatus?: string;
  processStatus?: string;
  interactionBoundary?: string;
}

export function deriveShellLifecycleLabels({
  sessionStatus,
  processStatus,
  interactionBoundary,
}: ShellLifecycleValues): { processLabel: string; activityLabel: string } {
  const session = normalize(sessionStatus);
  const processName = normalize(processStatus);
  const boundary = normalize(interactionBoundary);
  const process = PROCESS_PRESENTATION[processName];

  let processLabel = process?.processLabel ?? "";
  if (!processLabel) {
    if (session === "unavailable") {
      processLabel = "Session unavailable";
    } else if (session === "closed" || boundary === "terminal") {
      processLabel = "Session closed";
    } else if (session === "active" || processName === "running") {
      processLabel = "Session active";
    }
  }

  let activityLabel = "";
  if (session === "active" || processName === "running") {
    if (boundary === "output_available") {
      activityLabel = "Agent reviewing output";
    } else if (boundary === "quiet_boundary") {
      activityLabel = "Agent deciding next input";
    } else {
      activityLabel = "Process running";
    }
  }

  return { processLabel, activityLabel };
}
