/**
 * API helpers for provider-neutral LLM catalog, selection, and credentials.
 *
 * Callers use these helpers instead of hardcoding route payload shapes in UI
 * components, keeping the backend catalog as the source of provider policy.
 */

import { apiCall } from "@/lib/api-config";

import type {
  LLMModelCatalogResponse,
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

export async function saveLLMSelection(selection: SelectedLLMModel): Promise<SelectedLLMModel> {
  return apiCall<SelectedLLMModel>("/api/llm/selection", {
    method: "PUT",
    body: JSON.stringify(selection),
  });
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
  const response = await apiCall<ReportingLLMSelectionApiResponse>(
    "/api/llm/reporting-selection",
    {
      method: "PUT",
      body: JSON.stringify(selection),
    },
  );
  return mapReportingLLMSelectionResponse(response);
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

export function mapLLMSelectionResponse(response: LLMSelectionApiResponse): LLMSelection {
  return {
    provider: response.provider,
    model: response.model,
    selectionStatus: response.selection_status,
  };
}

export function mapReportingLLMSelectionResponse(
  response: ReportingLLMSelectionApiResponse,
): ReportingLLMSelection {
  return {
    provider: response.provider,
    model: response.model,
    reasoningEffort: response.reasoning_effort,
    selectionStatus: response.selection_status,
  };
}
