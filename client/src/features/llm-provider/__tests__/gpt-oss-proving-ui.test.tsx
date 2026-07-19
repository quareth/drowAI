// @vitest-environment jsdom
/**
 * Verifies the metadata-driven GPT-OSS proving UI flow.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ProviderSettingsSection from "../ProviderSettingsSection";
import type { LLMDeploymentRef, LLMModelCatalogResponse } from "../types";

const mocked = vi.hoisted(() => ({
  createLLMManagedConnection: vi.fn(),
  createLLMProvingConnection: vi.fn(),
  deleteLLMProviderCredential: vi.fn(),
  enableLLMManagedConnection: vi.fn(),
  enableLLMProvingConnection: vi.fn(),
  fetchLLMModelCatalog: vi.fn(),
  fetchLLMSelection: vi.fn(),
  fetchReportingLLMSelection: vi.fn(),
  refreshLLMManagedConnectionInventory: vi.fn(),
  saveLLMProviderCredential: vi.fn(),
  saveLLMDeploymentSelection: vi.fn(),
  saveReportingLLMSelection: vi.fn(),
  testLLMManagedConnection: vi.fn(),
  testLLMProviderCredential: vi.fn(),
  testLLMProvingConnection: vi.fn(),
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
  it("runs the metadata-driven proving lifecycle and selects the deployment", async () => {
    mocked.createLLMProvingConnection.mockResolvedValue({
      lifecycleState: "draft",
      connectionRef: catalog.providers[0].models[0].proving?.connectionRef,
      deploymentRef,
    });
    mocked.testLLMProvingConnection.mockResolvedValue({
      status: "passed",
      code: "verified",
      message: "GPT-OSS proving endpoint verified.",
      retryable: false,
      modelPresent: true,
      usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 },
    });
    mocked.enableLLMProvingConnection.mockResolvedValue({
      lifecycleState: "enabled",
      connectionRef: catalog.providers[0].models[0].proving?.connectionRef,
      deploymentRef,
      runnability: {
        status: "runnable",
        selectable: true,
        runnable: true,
      },
    });
    mocked.saveLLMDeploymentSelection.mockResolvedValue({
      provider: "registered-provider",
      model: "gpt-oss-20b",
      deploymentRef,
    });

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    expect(
      (await screen.findAllByText("GPT-OSS 20B OpenAI-compatible proving")).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("Verification: not_tested")).toBeTruthy();
    expect(screen.queryByText("Context: 128000 tokens")).toBeNull();
    expect(screen.queryByText("Pricing: unavailable")).toBeNull();
    expect(screen.queryByRole("button", { name: /select gpt-oss 20b/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Advanced model preferences" }));
    expect(screen.getByText("Context: 128000 tokens")).toBeTruthy();
    expect(screen.getByText("Pricing: unavailable")).toBeTruthy();
    expect(screen.queryByText("$0")).toBeNull();
    expect(screen.queryByLabelText(/endpoint/i)).toBeNull();
    expect(screen.queryByText(/marketplace/i)).toBeNull();
    expect(
      (screen.getByRole("button", { name: /select gpt-oss 20b/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);

    fireEvent.change(screen.getByLabelText("Proving API Key"), {
      target: { value: "sk-proving" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create draft/i }));
    await waitFor(() => {
      expect(mocked.createLLMProvingConnection).toHaveBeenCalledWith(
        "proving-preset",
        { api_key: "sk-proving" },
      );
    });

    fireEvent.click(screen.getByRole("button", { name: /test proving/i }));
    await waitFor(() => {
      expect(mocked.testLLMProvingConnection).toHaveBeenCalledWith(
        "proving-preset",
        {
          api_key: "sk-proving",
          connection_ref: catalog.providers[0].models[0].proving?.connectionRef,
          deployment_ref: deploymentRef,
        },
      );
    });
    fireEvent.click(screen.getByRole("button", { name: /enable/i }));
    await waitFor(() => {
      expect(mocked.enableLLMProvingConnection).toHaveBeenCalledWith(
        "proving-preset",
        {
          connection_ref: catalog.providers[0].models[0].proving?.connectionRef,
          deployment_ref: deploymentRef,
        },
      );
    });

    fireEvent.click(screen.getByRole("button", { name: /select gpt-oss 20b/i }));
    await waitFor(() => {
      expect(mocked.saveLLMDeploymentSelection).toHaveBeenCalledWith({
        deployment_ref: deploymentRef,
      });
    });
  });
});
