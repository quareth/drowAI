/**
 * API helpers for provider-neutral LLM catalog, selection, and credentials.
 *
 * Callers use these helpers instead of hardcoding route payload shapes in UI
 * components, keeping the backend catalog as the source of provider policy.
 */

import { apiCall } from "@/lib/api-config";

import type {
  LLMModelCatalogResponse,
  LLMDeploymentSelection,
  LLMManagedConnectionCreateRequest,
  LLMManagedConnectionEnableRequest,
  LLMManagedConnectionRefreshRequest,
  LLMManagedConnectionTestRequest,
  LLMProvingConnectionStatus,
  LLMProvingVerification,
  LLMProviderCredentialDeleteResponse,
  LLMProviderCredentialStatus,
  LLMProviderCredentialTestRequest,
  LLMProviderCredentialTestResponse,
  LLMProviderCredentialUpsert,
  LLMSelection,
  LLMSelectionApiResponse,
  ReportingLLMSelection,
  ReportingLLMSelectionApiResponse,
  ReportingLLMSelectionUpsert,
  SelectedLLMModel,
} from "./types";

export async function fetchLLMModelCatalog(): Promise<LLMModelCatalogResponse> {
  return apiCall<LLMModelCatalogResponse>("/api/llm/models");
}

export async function fetchLLMSelection(): Promise<LLMSelection> {
  const response = await apiCall<LLMSelectionApiResponse>("/api/llm/selection");
  return mapLLMSelectionResponse(response);
}

export async function saveLLMSelection(
  selection: SelectedLLMModel,
): Promise<LLMSelection> {
  return writeLLMSelection(selectionIdentityPayload(selection));
}

async function writeLLMSelection(
  selection: LLMDeploymentSelection | Pick<SelectedLLMModel, "provider" | "model">,
): Promise<LLMSelection> {
  const response = await apiCall<LLMSelectionApiResponse>("/api/llm/selection", {
    method: "PUT",
    body: JSON.stringify(selection),
  });
  return mapLLMSelectionResponse(response);
}

export async function fetchReportingLLMSelection(): Promise<ReportingLLMSelection> {
  const response = await apiCall<ReportingLLMSelectionApiResponse>(
    "/api/llm/reporting-selection",
  );
  return mapReportingLLMSelectionResponse(response);
}

export async function saveReportingLLMSelection(
  selection: ReportingLLMSelectionUpsert,
): Promise<ReportingLLMSelection> {
  const identity = selectionIdentityPayload(selection);
  const payload = {
    ...identity,
    ...(selection.reasoning_effort !== undefined
      ? { reasoning_effort: selection.reasoning_effort }
      : {}),
  };
  const response = await apiCall<ReportingLLMSelectionApiResponse>(
    "/api/llm/reporting-selection",
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
  return mapReportingLLMSelectionResponse(response);
}

function selectionIdentityPayload(
  selection: SelectedLLMModel,
): LLMDeploymentSelection | Pick<SelectedLLMModel, "provider" | "model"> {
  if (selection.deploymentRef) {
    return { deployment_ref: selection.deploymentRef };
  }
  return { provider: selection.provider, model: selection.model };
}

export async function fetchLLMProviderCredential(
  provider: string,
): Promise<LLMProviderCredentialStatus> {
  return apiCall<LLMProviderCredentialStatus>(
    `/api/llm/providers/${encodeURIComponent(provider)}/credential`,
  );
}

export async function saveLLMProviderCredential(
  provider: string,
  credential: LLMProviderCredentialUpsert,
): Promise<LLMProviderCredentialStatus> {
  return apiCall<LLMProviderCredentialStatus>(
    `/api/llm/providers/${encodeURIComponent(provider)}/credential`,
    {
      method: "PUT",
      body: JSON.stringify(credential),
    },
  );
}

export async function deleteLLMProviderCredential(
  provider: string,
): Promise<LLMProviderCredentialDeleteResponse> {
  return apiCall<LLMProviderCredentialDeleteResponse>(
    `/api/llm/providers/${encodeURIComponent(provider)}/credential`,
    { method: "DELETE" },
  );
}

export async function testLLMProviderCredential(
  provider: string,
  request: LLMProviderCredentialTestRequest = {},
): Promise<LLMProviderCredentialTestResponse> {
  return apiCall<LLMProviderCredentialTestResponse>(
    `/api/llm/providers/${encodeURIComponent(provider)}/credential/test`,
    {
      method: "POST",
      body: JSON.stringify(request),
    },
  );
}

export async function createLLMManagedConnection(
  presetId: string,
  request: LLMManagedConnectionCreateRequest = {},
): Promise<LLMProvingConnectionStatus> {
  return mapProvingConnectionStatus(
    await apiCall<Record<string, unknown>>(
      `/api/llm/connection-presets/${encodeURIComponent(presetId)}/connection`,
      {
        method: "POST",
        body: JSON.stringify(request),
      },
    ),
  );
}

export async function testLLMManagedConnection(
  presetId: string,
  request: LLMManagedConnectionTestRequest,
): Promise<LLMProvingVerification> {
  return mapProvingVerification(
    await apiCall<Record<string, unknown>>(
      `/api/llm/connection-presets/${encodeURIComponent(presetId)}/connection/test`,
      {
        method: "POST",
        body: JSON.stringify(request),
      },
    ),
  );
}

export async function refreshLLMManagedConnectionInventory(
  presetId: string,
  request: LLMManagedConnectionRefreshRequest,
): Promise<LLMProvingConnectionStatus> {
  return mapProvingConnectionStatus(
    await apiCall<Record<string, unknown>>(
      `/api/llm/connection-presets/${encodeURIComponent(presetId)}/connection/refresh`,
      {
        method: "POST",
        body: JSON.stringify(request),
      },
    ),
  );
}

export async function enableLLMManagedConnection(
  presetId: string,
  request: LLMManagedConnectionEnableRequest,
): Promise<LLMProvingConnectionStatus> {
  return mapProvingConnectionStatus(
    await apiCall<Record<string, unknown>>(
      `/api/llm/connection-presets/${encodeURIComponent(presetId)}/connection/enable`,
      {
        method: "POST",
        body: JSON.stringify(request),
      },
    ),
  );
}

export function mapLLMSelectionResponse(response: LLMSelectionApiResponse): LLMSelection {
  const selection: LLMSelection = {
    provider: response.provider,
    model: response.model,
  };
  if (response.selection_status) {
    selection.selectionStatus = response.selection_status;
  }
  if (response.deployment_ref) {
    selection.deploymentRef = response.deployment_ref;
  }
  return selection;
}

export function mapReportingLLMSelectionResponse(
  response: ReportingLLMSelectionApiResponse,
): ReportingLLMSelection {
  const selection: ReportingLLMSelection = {
    provider: response.provider,
    model: response.model,
    reasoningEffort: response.reasoning_effort,
    selectionStatus: response.selection_status,
  };
  if (response.deployment_ref) {
    selection.deploymentRef = response.deployment_ref;
  }
  return selection;
}

function mapProvingConnectionStatus(
  response: Record<string, unknown>,
): LLMProvingConnectionStatus {
  return {
    lifecycleState: String(
      response.lifecycleState ?? response.lifecycle_state ?? "unknown",
    ),
    connectionRef: readConnectionRef(response.connectionRef ?? response.connection_ref),
    deploymentRef: readDeploymentRef(response.deploymentRef ?? response.deployment_ref),
    verification: asRecord(response.verification)
      ? mapProvingVerification(asRecord(response.verification) as Record<string, unknown>)
      : null,
    runnability: asRunnability(response.runnability),
  };
}

function mapProvingVerification(
  response: Record<string, unknown>,
): LLMProvingVerification {
  return {
    status: String(response.status ?? "unknown"),
    code: String(response.code ?? "unknown"),
    message: String(response.message ?? ""),
    retryable: Boolean(response.retryable),
    observedAt: optionalString(response.observedAt ?? response.observed_at),
    expiresAt: optionalString(response.expiresAt ?? response.expires_at),
    modelPresent: optionalBoolean(response.modelPresent ?? response.model_present),
    usage: asUsageEvidence(response.usage),
  };
}

function readConnectionRef(value: unknown) {
  const record = asRecord(value);
  if (!record) {
    return null;
  }
  const connectionId = optionalString(record.connection_id);
  const expectedRevision = Number(record.expected_revision);
  if (!connectionId || !Number.isFinite(expectedRevision)) {
    return null;
  }
  return {
    connection_id: connectionId,
    expected_revision: expectedRevision,
  };
}

function readDeploymentRef(value: unknown) {
  const record = asRecord(value);
  if (!record) {
    return null;
  }
  const deploymentId = optionalString(record.deployment_id);
  const expectedRevision = Number(record.expected_revision);
  if (!deploymentId || !Number.isFinite(expectedRevision)) {
    return null;
  }
  return {
    deployment_id: deploymentId,
    expected_revision: expectedRevision,
  };
}

function asRunnability(value: unknown) {
  const record = asRecord(value);
  if (!record) {
    return null;
  }
  return {
    status: String(record.status ?? "unknown"),
    selectable: Boolean(record.selectable),
    runnable: Boolean(record.runnable),
    reason: optionalString(record.reason),
  };
}

function asUsageEvidence(value: unknown) {
  const record = asRecord(value);
  if (!record) {
    return null;
  }
  return {
    prompt_tokens: Number(record.prompt_tokens ?? 0),
    completion_tokens: Number(record.completion_tokens ?? 0),
    total_tokens: Number(record.total_tokens ?? 0),
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function optionalBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}
