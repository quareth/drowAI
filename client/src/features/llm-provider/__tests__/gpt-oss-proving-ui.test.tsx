// @vitest-environment jsdom
/**
 * Verifies internal GPT-OSS proving metadata is not exposed in Settings.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ProviderSettingsSection from "../ProviderSettingsSection";
import type { LLMDeploymentRef, LLMModelCatalogResponse } from "../types";

const mocked = vi.hoisted(() => ({
  saveLLMManagedConnection: vi.fn(),
  deleteLLMProviderCredential: vi.fn(),
  enableLLMManagedConnection: vi.fn(),
  fetchLLMModelCatalog: vi.fn(),
  fetchLLMSelection: vi.fn(),
  fetchReportingLLMSelection: vi.fn(),
  refreshLLMManagedConnectionInventory: vi.fn(),
  saveLLMProviderCredential: vi.fn(),
  saveReportingLLMSelection: vi.fn(),
  testLLMManagedConnection: vi.fn(),
  testLLMProviderCredential: vi.fn(),
}));

vi.mock("../api", () => mocked);

const deploymentRef: LLMDeploymentRef = {
  deployment_id: "11111111-1111-4111-8111-111111111111",
  expected_revision: 1,
};

const catalog: LLMModelCatalogResponse = {
  providers: [
    {
      id: "registered-provider",
      label: "Registered Provider",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "registered-provider",
        enabled: false,
        has_api_key: false,
      },
      defaultModel: "gpt-oss-20b",
      models: [
        {
          id: "gpt-oss-20b",
          canonicalModelId: "openai/gpt-oss-20b",
          exactWireModelId: null,
          label: "GPT-OSS 20B",
          apiSurface: "chat_completions",
          capabilities: ["chat", "context_window", "max_output_tokens", "usage_reporting"],
          contextWindowTokens: 128000,
          maxOutputTokens: 8192,
          reasoningEfforts: [],
          visibleReasoningEfforts: [],
          defaultReasoningEffort: null,
          defaultVisibleReasoningEffort: null,
          toolChoiceModes: [],
          structuredOutputStrategies: [],
          pricingStatus: "unavailable",
          deploymentRef,
          runnable: false,
          proving: {
            presetId: "proving-preset",
            displayName: "GPT-OSS 20B OpenAI-compatible proving",
            enabled: true,
            authMode: "bearer_api_key",
            userConfigFields: ["display_label", "api_key"],
            lifecycleState: "draft",
            connectionRef: {
              connection_id: "22222222-2222-4222-8222-222222222222",
              expected_revision: 1,
            },
            deploymentRef,
            verification: {
              status: "failed",
              code: "not_tested",
              message: "Verification has not run.",
              retryable: false,
            },
            runnability: {
              status: "capability_unknown",
              selectable: true,
              runnable: false,
              reason: "Usage evidence is required.",
            },
          },
        },
      ],
    },
  ],
};

function renderWithQueryClient(component: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      {component}
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.history.replaceState(null, "", "/settings?llm_proving=1");
  mocked.fetchLLMModelCatalog.mockResolvedValue(catalog);
  mocked.fetchLLMSelection.mockResolvedValue({
    provider: "registered-provider",
    model: "gpt-oss-20b",
    deploymentRef: null,
    selectionStatus: { status: "capability_unknown", selectable: true, runnable: false },
  });
  mocked.fetchReportingLLMSelection.mockResolvedValue({
    provider: null,
    model: null,
    reasoningEffort: null,
    selectionStatus: { status: "invalid_selection", selectable: false, runnable: false },
  });
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
  cleanup();
  vi.clearAllMocks();
});

describe("GPT-OSS proving UI", () => {
  it("ignores proving controls even when legacy metadata and query flags exist", async () => {
    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    await waitFor(() => {
      expect(mocked.fetchLLMModelCatalog).toHaveBeenCalled();
    });
    expect(screen.queryByText("GPT-OSS 20B OpenAI-compatible proving")).toBeNull();
    expect(screen.queryByText("Verification: not_tested")).toBeNull();
    expect(screen.queryByText("Context: 128000 tokens")).toBeNull();
    expect(screen.queryByText("Pricing: unavailable")).toBeNull();
    expect(screen.queryByText(/capability evidence|lifecycle|runnability/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /advanced model preferences/i })).toBeNull();
  });
});
