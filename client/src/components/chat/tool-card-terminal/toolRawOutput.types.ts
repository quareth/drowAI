/**
 * Local contracts for tool-card raw output resolution and rendering readiness.
 *
 * These types cover the raw-output batch payload and the resolver hook state
 * machine used by chat tool cards.
 */

export interface ToolRawOutputBatchEntry {
  status?: string;
  reason?: string;
  output_text?: string;
  command_artifact_id?: string | null;
  stdout_artifact_id?: string | null;
  stderr_artifact_id?: string | null;
  message?: string;
}

export interface ToolRawOutputBatchPayload {
  results?: Record<string, ToolRawOutputBatchEntry>;
  missing?: string[];
}

export type ToolRawOutputNotAvailableReason =
  | "missing_identifiers"
  | "execution_not_found"
  | "missing_output_artifacts"
  | "artifact_not_found"
  | "artifact_content_unavailable";

export interface ToolRawOutputReadyState {
  status: "ready";
  outputText: string;
  commandArtifactId?: string;
  stdoutArtifactId?: string;
  stderrArtifactId?: string;
}

export interface ToolRawOutputNotAvailableState {
  status: "not_available";
  reason: ToolRawOutputNotAvailableReason;
  commandArtifactId?: string;
  stdoutArtifactId?: string;
  stderrArtifactId?: string;
}

export interface ToolRawOutputErrorState {
  status: "error";
  message: string;
}

export type ToolRawOutputState =
  | { status: "idle" }
  | { status: "loading" }
  | ToolRawOutputReadyState
  | ToolRawOutputNotAvailableState
  | ToolRawOutputErrorState;

export type ToolRawOutputStatus = ToolRawOutputState["status"];

export interface UseToolRawOutputOptions {
  taskId?: number | string | null;
  toolCallId?: string | null;
  enabled?: boolean;
}

