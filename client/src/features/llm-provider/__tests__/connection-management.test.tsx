// @vitest-environment jsdom
/**
 * Verifies scalable provider settings separate connection config from deployment selection.
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
  saveLLMDeploymentSelection: vi.fn(),
  saveLLMProviderCredential: vi.fn(),
  saveReportingLLMSelection: vi.fn(),
  testLLMManagedConnection: vi.fn(),
  testLLMProviderCredential: vi.fn(),
  testLLMProvingConnection: vi.fn(),
}));

vi.mock("../api", () => mocked);

const deploymentRef: LLMDeploymentRef = {
  deployment_id: "11111111-1111-4111-8111-111111111111",
  expected_revision: 2,
};

const catalog: LLMModelCatalogResponse = {
  providers: [
    {
      id: "metadata-provider",
      label: "Metadata Provider",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "metadata-provider",
        enabled: true,
        has_api_key: true,
      },
      defaultModel: "metadata/model",
      models: [
        {
          id: "metadata/model",
          label: "Metadata Model",
          apiSurface: "chat_completions",
          capabilities: ["chat", "usage_reporting"],
          contextWindowTokens: 64000,
          maxOutputTokens: 4096,
          reasoningEfforts: [],
          visibleReasoningEfforts: [],
          defaultReasoningEffort: null,
          defaultVisibleReasoningEffort: null,
          toolChoiceModes: ["auto"],
          structuredOutputStrategies: [],
          pricingStatus: "unavailable",
          deploymentRef,
          runnable: true,
          proving: {
            presetId: "metadata-preset",
            displayName: "Metadata Connection",
            enabled: true,
            authMode: "bearer_api_key",
            userConfigFields: ["api_key"],
            lifecycleState: "enabled",
            connectionRef: {
              connection_id: "22222222-2222-4222-8222-222222222222",
              expected_revision: 3,
            },
            deploymentRef,
            verification: {
              status: "passed",
              code: "verified",
              message: "Verified",
              retryable: false,
            },
            runnability: {
              status: "runnable",
              selectable: true,
              runnable: true,
              reason: null,
            },
          },
        },
      ],
    },
  ],
};

const managedConnectionRef = {
  connection_id: "33333333-3333-4333-8333-333333333333",
  expected_revision: 4,
};

const managedCatalog: LLMModelCatalogResponse = {
  providers: [
    {
      id: "custom_openai_compatible_chat",
      label: "Custom OpenAI-compatible",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "custom_openai_compatible_chat",
        enabled: false,
        has_api_key: true,
      },
      defaultModel: "team/model",
      models: [
        {
          id: "team/model",
          label: "Team Model",
          apiSurface: "chat_completions",
          capabilities: ["chat"],
          contextWindowTokens: 128000,
          maxOutputTokens: 10000,
          reasoningEfforts: [],
          visibleReasoningEfforts: [],
          defaultReasoningEffort: null,
          defaultVisibleReasoningEffort: null,
          toolChoiceModes: ["auto"],
          structuredOutputStrategies: [],
          pricingStatus: "unavailable",
          deploymentRef: null,
          runnable: false,
          connection: {
            presetId: "custom_openai_compatible_chat",
            displayName: "Custom OpenAI-compatible HTTPS endpoint",
            enabled: true,
            authMode: "bearer_api_key",
            userConfigFields: ["api_key"],
            configFields: [
              {
                name: "api_key",
                label: "API key",
                fieldType: "password",
                required: true,
                secret: true,
              },
            ],
            lifecycleState: "draft",
            connectionRef: managedConnectionRef,
            deploymentRef: null,
            verification: {
              status: "failed",
              code: "not_tested",
              message: "Verification has not run.",
              retryable: false,
            },
            runnability: {
              status: "deployment_missing",
              selectable: true,
              runnable: false,
              reason: "Deployment model registration is required.",
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
  mocked.fetchLLMModelCatalog.mockResolvedValue(catalog);
  mocked.fetchLLMSelection.mockResolvedValue({
    provider: "metadata-provider",
    model: "metadata/model",
    deploymentRef,
    selectionStatus: { status: "selectable", selectable: true, runnable: true },
  });
  mocked.fetchReportingLLMSelection.mockResolvedValue({
    provider: null,
    model: null,
    reasoningEffort: null,
    selectionStatus: { status: "invalid_selection", selectable: false, runnable: false },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Connection management", () => {
  it("keeps connection configuration separate from workload deployment selection", async () => {
    mocked.saveLLMDeploymentSelection.mockResolvedValue({
      provider: "metadata-provider",
      model: "metadata/model",
      deploymentRef,
      selectionStatus: { status: "selectable", selectable: true, runnable: true },
    });

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    expect(await screen.findByText("Connection configuration")).toBeTruthy();
    expect(screen.getByText("Workload deployment")).toBeTruthy();
    expect(screen.getByLabelText("Proving API Key")).toBeTruthy();
    expect(screen.queryByLabelText(/endpoint|base url|headers/i)).toBeNull();
    expect(screen.queryByText(/marketplace|fallback|tenant admin/i)).toBeNull();
    expect(screen.getByText("Pricing: unavailable")).toBeTruthy();
    expect(screen.queryByText("$0")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /select metadata model/i }));

    await waitFor(() => {
      expect(mocked.saveLLMDeploymentSelection).toHaveBeenCalledWith({
        deployment_ref: deploymentRef,
      });
    });
  });

  it("refreshes managed inventory through the backend connection API", async () => {
    mocked.fetchLLMModelCatalog.mockResolvedValue(managedCatalog);
    mocked.refreshLLMManagedConnectionInventory.mockResolvedValue({
      lifecycleState: "draft",
      connectionRef: managedConnectionRef,
      deploymentRef: null,
      verification: null,
      runnability: {
        status: "deployment_missing",
        selectable: true,
        runnable: false,
        reason: "Deployment model registration is required.",
      },
    });

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    fireEvent.change(await screen.findByLabelText("API key"), {
      target: { value: "sk-managed" },
    });
    fireEvent.click(screen.getByRole("button", { name: /refresh inventory/i }));

    await waitFor(() => {
      expect(mocked.refreshLLMManagedConnectionInventory).toHaveBeenCalledWith(
        "custom_openai_compatible_chat",
        {
          api_key: "sk-managed",
          connection_ref: managedConnectionRef,
        },
      );
    });
  });
});
