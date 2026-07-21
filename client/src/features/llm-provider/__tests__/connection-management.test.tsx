// @vitest-environment jsdom
/**
 * Verifies the intentionally limited GPT-OSS provider settings experience.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConnectionSettingsPanel from "../ConnectionSettingsPanel";
import ProviderSettingsSection from "../ProviderSettingsSection";
import type { LLMDeploymentRef, LLMModelCatalogResponse } from "../types";

const mocked = vi.hoisted(() => ({
  createLLMManagedConnection: vi.fn(),
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
      id: "ollama_openai_compatible_chat",
      label: "Ollama",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "ollama_openai_compatible_chat",
        enabled: false,
        has_api_key: true,
      },
      defaultModel: "openai/gpt-oss-20b",
      models: [
        {
          id: "gpt-oss:20b",
          canonicalModelId: "openai/gpt-oss-20b",
          label: "GPT-OSS 20B via Ollama",
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
            presetId: "ollama_openai_compatible_chat",
            displayName: "Ollama-compatible HTTPS endpoint",
            enabled: true,
            authMode: "bearer_api_key",
            userConfigFields: ["display_label", "base_url", "api_key"],
            configFields: [
              {
                name: "display_label",
                label: "Display name",
                fieldType: "text",
                required: false,
                secret: false,
              },
              {
                name: "base_url",
                label: "Base URL",
                fieldType: "url",
                required: true,
                secret: false,
              },
              {
                name: "api_key",
                label: "API key",
                fieldType: "password",
                required: true,
                secret: true,
              },
              {
                name: "wire_model_id",
                label: "Model ID",
                fieldType: "text",
                required: true,
                secret: false,
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
  window.history.replaceState(null, "", "/settings");
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
  window.history.replaceState(null, "", "/");
  cleanup();
  vi.clearAllMocks();
});

describe("Connection management", () => {
  it("shows one supported NVIDIA card while ignoring legacy inventory rows", async () => {
    const nimConnectionRef = {
      connection_id: "44444444-4444-4444-8444-444444444444",
      expected_revision: 7,
    };
    const nimDeploymentRef = {
      deployment_id: "55555555-5555-4555-8555-555555555555",
      expected_revision: 8,
    };
    const secondNimDeploymentRef = {
      deployment_id: "66666666-6666-4666-8666-666666666666",
      expected_revision: 9,
    };
    const sharedNimConnection = {
      presetId: "nvidia_nim_openai_compatible_chat",
      displayName: "NVIDIA NIM Endpoint",
      enabled: true,
      authMode: "bearer_api_key",
      userConfigFields: ["api_key"],
      configFields: [
        {
          name: "api_key",
          label: "API key",
          fieldType: "password" as const,
          required: true,
          secret: true,
        },
      ],
    };
    const duplicateCatalog: LLMModelCatalogResponse = {
      providers: [
        {
          id: "openai",
          label: "OpenAI",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "openai",
            enabled: true,
            has_api_key: true,
          },
          defaultModel: "gpt-4.1",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "gpt-4.1",
              label: "GPT-4.1",
              deploymentRef: null,
              connection: null,
              proving: null,
            },
          ],
        },
        {
          id: "anthropic",
          label: "Anthropic",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "anthropic",
            enabled: true,
            has_api_key: true,
          },
          defaultModel: "claude-sonnet-4-6",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "claude-sonnet-4-6",
              label: "Claude Sonnet 4.6",
              deploymentRef: null,
              connection: null,
              proving: null,
            },
          ],
        },
        {
          id: "nvidia_nim_openai_compatible_chat",
          label: "NVIDIA NIM",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "nvidia_nim_openai_compatible_chat",
            enabled: true,
            has_api_key: true,
          },
          defaultModel: "openai/gpt-oss-20b",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "meta/llama-3.1-8b-instruct",
              canonicalModelId: "meta/llama-3.1-8b-instruct",
              exactWireModelId: "meta/llama-3.1-8b-instruct",
              label: "Llama 3.1 8B via NVIDIA NIM",
              deploymentRef: null,
              runnable: false,
              connection: {
                ...sharedNimConnection,
                lifecycleState: "not_created",
                connectionRef: null,
                deploymentRef: null,
                verification: null,
                runnability: {
                  status: "not_created",
                  selectable: true,
                  runnable: false,
                  reason: "Connection configuration is required.",
                },
              },
              proving: null,
            },
            {
              ...catalog.providers[0].models[0],
              id: "openai/gpt-oss-20b",
              canonicalModelId: "openai/gpt-oss-20b",
              exactWireModelId: "openai/gpt-oss-20b",
              label: "GPT-OSS 20B via NVIDIA",
              deploymentRef: nimDeploymentRef,
              runnable: true,
              connection: {
                ...sharedNimConnection,
                lifecycleState: "enabled",
                connectionRef: nimConnectionRef,
                deploymentRef: nimDeploymentRef,
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
              proving: null,
            },
            {
              ...catalog.providers[0].models[0],
              id: "mistralai/mixtral-8x7b-instruct",
              canonicalModelId: "mistralai/mixtral-8x7b-instruct",
              exactWireModelId: "mistralai/mixtral-8x7b-instruct",
              label: "Mixtral 8x7B via NVIDIA NIM",
              deploymentRef: secondNimDeploymentRef,
              runnable: true,
              connection: {
                ...sharedNimConnection,
                lifecycleState: "enabled",
                connectionRef: nimConnectionRef,
                deploymentRef: secondNimDeploymentRef,
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
              proving: null,
            },
          ],
        },
        managedCatalog.providers[0],
      ],
    };
    mocked.fetchLLMModelCatalog.mockResolvedValue(duplicateCatalog);
    mocked.createLLMManagedConnection.mockResolvedValue({
      lifecycleState: "draft",
      connectionRef: null,
      deploymentRef: null,
      verification: null,
      runnability: {
        status: "not_created",
        selectable: true,
        runnable: false,
        reason: "Connection configuration is required.",
      },
    });
    mocked.testLLMManagedConnection.mockResolvedValue({
      status: "passed",
      code: "verified",
      message: "Connection verified.",
      retryable: false,
    });
    mocked.refreshLLMManagedConnectionInventory.mockResolvedValue({
      lifecycleState: "enabled",
      connectionRef: nimConnectionRef,
      deploymentRef: nimDeploymentRef,
      verification: null,
      runnability: {
        status: "runnable",
        selectable: true,
        runnable: true,
        reason: null,
      },
    });
    mocked.enableLLMManagedConnection.mockResolvedValue({
      lifecycleState: "enabled",
      connectionRef: nimConnectionRef,
      deploymentRef: nimDeploymentRef,
      verification: null,
      runnability: {
        status: "runnable",
        selectable: true,
        runnable: true,
        reason: null,
      },
    });

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    expect(await screen.findByText("NVIDIA")).toBeTruthy();
    expect(screen.getAllByText("NVIDIA")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /update nvidia/i })).toBeTruthy();
    const providerCard = screen.getByRole("group", { name: "NVIDIA provider settings" });
    expect(within(providerCard).getByLabelText("NVIDIA status: Ready")).toBeTruthy();
    expect(within(providerCard).getByRole("button", { name: "Show API key" })).toBeTruthy();
    expect(screen.getAllByText("OpenAI").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Anthropic").length).toBeGreaterThan(0);

    expect(screen.queryByText("Llama 3.1 8B via NVIDIA NIM")).toBeNull();
    expect(screen.queryByText("Mixtral 8x7B via NVIDIA NIM")).toBeNull();
    expect(screen.queryByText(/capability evidence|lifecycle|runnability/i)).toBeNull();

    fireEvent.change(within(providerCard).getByLabelText("API Key"), {
      target: { value: "sk-nim" },
    });
    fireEvent.click(screen.getByRole("button", { name: /update nvidia/i }));

    await waitFor(() => {
      expect(mocked.createLLMManagedConnection).toHaveBeenCalledWith(
        "nvidia_nim_openai_compatible_chat",
        expect.objectContaining({
          wire_model_id: "openai/gpt-oss-20b",
          canonical_model_id: "openai/gpt-oss-20b",
        }),
      );
      expect(mocked.testLLMManagedConnection).toHaveBeenCalledWith(
        "nvidia_nim_openai_compatible_chat",
        {
          api_key: "sk-nim",
          connection_ref: nimConnectionRef,
        },
      );
      expect(mocked.enableLLMManagedConnection).toHaveBeenCalledWith(
        "nvidia_nim_openai_compatible_chat",
        {
          connection_ref: nimConnectionRef,
          deployment_ref: nimDeploymentRef,
        },
      );
    });
  });

  it("deduplicates local route cards while preserving Ollama and vLLM", async () => {
    const duplicateAdvancedCatalog: LLMModelCatalogResponse = {
      providers: [
        {
          ...managedCatalog.providers[0],
          models: [
            managedCatalog.providers[0].models[0],
            {
              ...managedCatalog.providers[0].models[0],
              id: "team/second-model",
              label: "Team Second Model",
              exactWireModelId: "team/second-model",
            },
          ],
        },
        {
          ...managedCatalog.providers[0],
          id: "vllm_openai_compatible_chat",
          label: "vLLM",
          credential: {
            ...managedCatalog.providers[0].credential,
            provider: "vllm_openai_compatible_chat",
          },
          defaultModel: "openai/gpt-oss-20b",
          models: [
            {
              ...managedCatalog.providers[0].models[0],
              id: "gpt-oss-20b",
              label: "GPT-OSS 20B via vLLM",
              exactWireModelId: "gpt-oss-20b",
              connection: {
                ...managedCatalog.providers[0].models[0].connection!,
                presetId: "vllm_openai_compatible_chat",
                displayName: "vLLM-compatible HTTPS endpoint",
              },
            },
          ],
        },
      ],
    };
    mocked.fetchLLMModelCatalog.mockResolvedValue(duplicateAdvancedCatalog);

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    fireEvent.click(await screen.findByRole("button", {
      name: "Local & self-hosted",
    }));

    expect(screen.getAllByText("Ollama")).toHaveLength(1);
    expect(screen.getAllByText("vLLM")).toHaveLength(1);
  });

  it("separates hosted API-key setup from advanced endpoint setup", async () => {
    const hostedAndAdvancedCatalog: LLMModelCatalogResponse = {
      providers: [
        {
          id: "openai",
          label: "OpenAI",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "openai",
            enabled: false,
            has_api_key: false,
          },
          defaultModel: "gpt-4.1",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "gpt-4.1",
              label: "GPT-4.1",
              deploymentRef: null,
              proving: catalog.providers[0].models[0].proving,
              connection: null,
            },
          ],
        },
        {
          id: "anthropic",
          label: "Anthropic",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "anthropic",
            enabled: false,
            has_api_key: false,
          },
          defaultModel: "claude-sonnet-4-6",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "claude-sonnet-4-6",
              label: "Claude Sonnet 4.6",
              deploymentRef: null,
              proving: null,
              connection: null,
            },
          ],
        },
        {
          id: "huggingface_openai_compatible_chat",
          label: "Hugging Face",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "huggingface_openai_compatible_chat",
            enabled: false,
            has_api_key: false,
          },
          defaultModel: "openai/gpt-oss-20b:fireworks-ai",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "openai/gpt-oss-20b:fireworks-ai",
              canonicalModelId: "openai/gpt-oss-20b",
              exactWireModelId: "openai/gpt-oss-20b:fireworks-ai",
              label: "GPT-OSS 20B via Hugging Face",
              deploymentRef: null,
              connection: {
                presetId: "huggingface_openai_compatible_chat",
                displayName: "Hugging Face Router",
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
                lifecycleState: "not_created",
                connectionRef: null,
                deploymentRef: null,
                verification: null,
                runnability: {
                  status: "not_created",
                  selectable: true,
                  runnable: false,
                  reason: "Connection configuration is required.",
                },
              },
              proving: null,
            },
          ],
        },
        {
          id: "nvidia_nim_openai_compatible_chat",
          label: "NVIDIA NIM",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "nvidia_nim_openai_compatible_chat",
            enabled: false,
            has_api_key: false,
          },
          defaultModel: "openai/gpt-oss-20b",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "openai/gpt-oss-20b",
              canonicalModelId: "openai/gpt-oss-20b",
              exactWireModelId: "openai/gpt-oss-20b",
              label: "GPT-OSS 20B via NVIDIA NIM",
              deploymentRef: null,
              connection: {
                presetId: "nvidia_nim_openai_compatible_chat",
                displayName: "NVIDIA NIM Endpoint",
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
                lifecycleState: "not_created",
                connectionRef: null,
                deploymentRef: null,
                verification: null,
                runnability: {
                  status: "not_created",
                  selectable: true,
                  runnable: false,
                  reason: "Connection configuration is required.",
                },
              },
              proving: null,
            },
          ],
        },
        managedCatalog.providers[0],
      ],
    };
    mocked.fetchLLMModelCatalog.mockResolvedValue(hostedAndAdvancedCatalog);

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    expect(await screen.findByRole("heading", { name: "AI providers" })).toBeTruthy();
    expect(screen.getAllByRole("heading", { level: 3 })[0].textContent).toBe("Reporting model");
    expect(screen.getAllByRole("heading", { level: 3 })[1].textContent).toBe("AI providers");
    expect(screen.getByRole("heading", { name: "Open models" })).toBeTruthy();
    expect(screen.getByText("Connect open models through hosted providers.")).toBeTruthy();
    expect(screen.queryByText("Connect GPT-OSS 20B to a hosted service.")).toBeNull();
    expect(screen.getByText("Reporting model")).toBeTruthy();
    expect(screen.queryByText("Workload deployment")).toBeNull();
    expect(screen.queryByRole("button", { name: "Advanced model preferences" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Local & self-hosted" }).getAttribute(
        "aria-expanded",
      ),
    ).toBe("false");
    expect(screen.getAllByText("OpenAI").length).toBeGreaterThan(0);
    expect(screen.getByText("Anthropic")).toBeTruthy();
    expect(screen.getByText("Usage is billed by Anthropic for the selected model.")).toBeTruthy();
    expect(screen.getByText("Usage is billed by OpenAI for the selected model.")).toBeTruthy();
    expect(screen.getAllByText("Hugging Face").length).toBeGreaterThan(0);
    expect(screen.getAllByText("NVIDIA").length).toBeGreaterThan(0);
    expect(screen.getByText("Credits and pay-as-you-go usage apply.")).toBeTruthy();
    expect(screen.getByText("Free development and prototyping access has usage limits.")).toBeTruthy();
    expect(screen.queryByText("Custom OpenAI-compatible HTTPS endpoint")).toBeNull();
    expect(screen.queryByLabelText("Base URL")).toBeNull();
    expect(screen.queryByText("GPT-OSS 20B OpenAI-compatible proving")).toBeNull();
    expect(screen.queryByRole("button", { name: /create draft/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /test proving|test connection/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /refresh inventory/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^enable$/i })).toBeNull();
    expect(screen.queryByText(/Verification:/)).toBeNull();
    expect(screen.queryByText(/capability evidence|lifecycle|runnability|deployment/i)).toBeNull();
    expect(screen.queryByText(/wire|canonical|adapter|provenance/i)).toBeNull();
    expect(screen.queryByText(/\$0|free production/i)).toBeNull();
  });

  it("connects hosted credentials and managed providers with one visible action", async () => {
    const hostedCatalog: LLMModelCatalogResponse = {
      providers: [
        {
          id: "openai",
          label: "OpenAI",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "openai",
            enabled: false,
            has_api_key: false,
          },
          defaultModel: "gpt-4.1",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "gpt-4.1",
              label: "GPT-4.1",
              proving: null,
              connection: null,
            },
          ],
        },
        {
          id: "huggingface_openai_compatible_chat",
          label: "Hugging Face",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "huggingface_openai_compatible_chat",
            enabled: false,
            has_api_key: false,
          },
          defaultModel: "openai/gpt-oss-20b:fireworks-ai",
          models: [
            {
              ...catalog.providers[0].models[0],
              id: "openai/gpt-oss-20b:fireworks-ai",
              canonicalModelId: "openai/gpt-oss-20b",
              exactWireModelId: "openai/gpt-oss-20b:fireworks-ai",
              label: "GPT-OSS 20B via Hugging Face",
              deploymentRef: null,
              runnable: false,
              connection: {
                presetId: "huggingface_openai_compatible_chat",
                displayName: "Hugging Face Router",
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
                lifecycleState: "not_created",
                connectionRef: null,
                deploymentRef: null,
                verification: null,
                runnability: {
                  status: "not_created",
                  selectable: true,
                  runnable: false,
                  reason: "Connection configuration is required.",
                },
              },
              proving: null,
            },
          ],
        },
      ],
    };
    mocked.fetchLLMModelCatalog.mockResolvedValue(hostedCatalog);
    mocked.saveLLMProviderCredential.mockResolvedValue({
      user_id: 1,
      provider: "openai",
      enabled: true,
      has_api_key: true,
    });
    mocked.testLLMProviderCredential.mockResolvedValue({
      provider: "openai",
      status: "passed",
      message: "Connection verified.",
      model_count: 1,
    });
    mocked.createLLMManagedConnection.mockResolvedValue({
      lifecycleState: "draft",
      connectionRef: managedConnectionRef,
      deploymentRef,
      verification: null,
      runnability: {
        status: "not_created",
        selectable: true,
        runnable: false,
        reason: "Connection configuration is required.",
      },
    });
    mocked.testLLMManagedConnection.mockResolvedValue({
      status: "passed",
      code: "verified",
      message: "Connection verified.",
      retryable: false,
    });
    mocked.refreshLLMManagedConnectionInventory.mockResolvedValue({
      lifecycleState: "draft",
      connectionRef: managedConnectionRef,
      deploymentRef,
      verification: null,
      runnability: {
        status: "capability_unknown",
        selectable: true,
        runnable: false,
        reason: "Usage evidence is required.",
      },
    });
    mocked.enableLLMManagedConnection.mockResolvedValue({
      lifecycleState: "enabled",
      connectionRef: managedConnectionRef,
      deploymentRef,
      verification: null,
      runnability: {
        status: "runnable",
        selectable: true,
        runnable: true,
        reason: null,
      },
    });

    const onSuccess = vi.fn();
    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={onSuccess}
        onError={() => undefined}
      />,
    );

    const openAICard = await screen.findByRole("group", {
      name: "OpenAI provider settings",
    });
    fireEvent.change(within(openAICard).getByLabelText("API Key"), {
      target: { value: "sk-openai" },
    });
    fireEvent.click(screen.getByRole("button", { name: /connect openai/i }));

    await waitFor(() => {
      expect(mocked.saveLLMProviderCredential).toHaveBeenCalledWith("openai", {
        api_key: "sk-openai",
        enabled: true,
      });
      expect(mocked.testLLMProviderCredential).toHaveBeenCalledWith("openai", {
        api_key: "sk-openai",
      });
    });

    const huggingFaceCard = screen.getByRole("group", {
      name: "Hugging Face provider settings",
    });
    fireEvent.change(within(huggingFaceCard).getByLabelText("API Key"), {
      target: { value: "sk-hf" },
    });
    fireEvent.click(screen.getByRole("button", { name: /connect hugging face/i }));

    await waitFor(() => {
      expect(mocked.createLLMManagedConnection).toHaveBeenCalledWith(
        "huggingface_openai_compatible_chat",
        {
          api_key: "sk-hf",
          display_label: null,
          base_url: null,
          wire_model_id: "openai/gpt-oss-20b:fireworks-ai",
          model_label: "GPT-OSS 20B via Hugging Face",
          canonical_model_id: "openai/gpt-oss-20b",
        },
      );
      expect(mocked.testLLMManagedConnection).toHaveBeenCalledWith(
        "huggingface_openai_compatible_chat",
        {
          api_key: "sk-hf",
          connection_ref: managedConnectionRef,
        },
      );
      expect(mocked.refreshLLMManagedConnectionInventory).not.toHaveBeenCalled();
      expect(mocked.enableLLMManagedConnection).toHaveBeenCalledWith(
        "huggingface_openai_compatible_chat",
        {
          connection_ref: managedConnectionRef,
          deployment_ref: deploymentRef,
        },
      );
      expect(onSuccess).toHaveBeenCalledWith(
        "Hugging Face connected",
        "GPT-OSS 20B is ready.",
      );
    });
    expect(
      mocked.createLLMManagedConnection.mock.invocationCallOrder[0],
    ).toBeLessThan(mocked.testLLMManagedConnection.mock.invocationCallOrder[0]);
    expect(
      mocked.testLLMManagedConnection.mock.invocationCallOrder[0],
    ).toBeLessThan(mocked.enableLLMManagedConnection.mock.invocationCallOrder[0]);
  });

  it("does not expose internal deployment and capability controls", async () => {
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
    expect(screen.queryByRole("button", { name: "Advanced model preferences" })).toBeNull();
    expect(screen.getByText("Reporting model")).toBeTruthy();
    expect(screen.queryByText("Workload deployment")).toBeNull();
    expect(screen.queryByPlaceholderText("Search deployments")).toBeNull();
    expect(screen.queryByText(/capability evidence|lifecycle|runnability/i)).toBeNull();
    expect(screen.queryByText(/context:|output:|pricing:/i)).toBeNull();
  });

  it("keeps advanced endpoint fields collapsed until explicitly opened", async () => {
    mocked.fetchLLMModelCatalog.mockResolvedValue(managedCatalog);

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    const disclosure = await screen.findByRole("button", {
      name: "Local & self-hosted",
    });
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Ollama")).toBeNull();
    expect(screen.queryByLabelText("Base URL")).toBeNull();
    expect(screen.queryByLabelText("Model name")).toBeNull();

    fireEvent.click(disclosure);

    expect(disclosure.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Ollama")).toBeTruthy();
    expect(screen.getByLabelText("Base URL")).toBeTruthy();
    expect(screen.getByLabelText("API Key")).toBeTruthy();
    expect(screen.getByLabelText("Model name")).toBeTruthy();
    expect(screen.queryByLabelText("Display name")).toBeNull();
    expect(screen.queryByRole("button", { name: /create draft/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /test connection/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /refresh inventory/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^enable$/i })).toBeNull();
    expect(screen.getByRole("button", { name: /update ollama/i })).toBeTruthy();
  });

  it("does not start the managed workflow while required visible fields are missing", async () => {
    renderWithQueryClient(
      <ConnectionSettingsPanel
        model={managedCatalog.providers[0].models[0]}
        connection={managedCatalog.providers[0].models[0].connection!}
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    const action = screen.getByRole("button", {
      name: /update ollama-compatible https endpoint/i,
    }) as HTMLButtonElement;
    expect(action.disabled).toBe(true);
    fireEvent.click(action);
    expect(mocked.createLLMManagedConnection).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://llm.example.test/team" },
    });
    fireEvent.change(screen.getByLabelText("API Key"), {
      target: { value: "sk-required-placeholder" },
    });

    expect(action.disabled).toBe(true);
    fireEvent.click(action);
    expect(mocked.createLLMManagedConnection).not.toHaveBeenCalled();
  });


  it("submits the GPT-OSS identity with a self-hosted serving model name", async () => {
    mocked.fetchLLMModelCatalog.mockResolvedValue(managedCatalog);
    mocked.createLLMManagedConnection.mockResolvedValue({
      lifecycleState: "draft",
      connectionRef: managedConnectionRef,
      deploymentRef: null,
      verification: null,
      runnability: {
        status: "capability_unknown",
        selectable: true,
        runnable: false,
        reason: "Usage evidence is required.",
      },
    });
    mocked.testLLMManagedConnection.mockResolvedValue({
      status: "failed",
      code: "not_tested",
      message: "Verification has not run.",
      retryable: false,
    });
    mocked.refreshLLMManagedConnectionInventory.mockResolvedValue({
      lifecycleState: "draft",
      connectionRef: managedConnectionRef,
      deploymentRef,
      verification: null,
      runnability: {
        status: "capability_unknown",
        selectable: true,
        runnable: false,
        reason: "Usage evidence is required.",
      },
    });

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    fireEvent.click(await screen.findByRole("button", {
      name: "Local & self-hosted",
    }));
    fireEvent.change(await screen.findByLabelText("API Key"), {
      target: { value: "sk-managed" },
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://llm.example.test/team" },
    });
    fireEvent.change(screen.getByLabelText("Model name"), {
      target: { value: "team/model" },
    });
    fireEvent.click(screen.getByRole("button", { name: /update ollama/i }));

    await waitFor(() => {
      expect(mocked.createLLMManagedConnection).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        expect.objectContaining({
          canonical_model_id: "openai/gpt-oss-20b",
          wire_model_id: "team/model",
        }),
      );
      expect(mocked.refreshLLMManagedConnectionInventory).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          api_key: "sk-managed",
          connection_ref: managedConnectionRef,
        },
      );
      expect(mocked.enableLLMManagedConnection).not.toHaveBeenCalled();
    });
  });
});
