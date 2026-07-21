// @vitest-environment jsdom
/**
 * Verifies model-first picker behavior from backend catalog data.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProviderModelMenu } from "../ProviderModelMenu";
import type { LLMModelCatalogResponse } from "../types";

const catalog: LLMModelCatalogResponse = {
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
        masked_api_key: "sk-...1234",
      },
      defaultModel: "gpt-5-mini",
      models: [
        {
          id: "gpt-5-mini",
          label: "GPT-5 mini",
          apiSurface: "responses",
          capabilities: ["chat", "reasoning_effort"],
          contextWindowTokens: 128000,
          maxOutputTokens: 32000,
          reasoningEfforts: ["minimal", "low", "medium", "high"],
          visibleReasoningEfforts: ["low", "medium", "high"],
          defaultReasoningEffort: "minimal",
          defaultVisibleReasoningEffort: "medium",
          toolChoiceModes: ["auto"],
          structuredOutputStrategies: ["native_schema"],
          pricingStatus: "priced",
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
        masked_api_key: "sk-ant-...1234",
      },
      defaultModel: "claude-sonnet-4-6",
      models: [
        {
          id: "claude-sonnet-4-6",
          label: "Claude Sonnet 4.6",
          apiSurface: "messages",
          capabilities: ["chat"],
          contextWindowTokens: 1000000,
          maxOutputTokens: 64000,
          reasoningEfforts: [],
          visibleReasoningEfforts: [],
          defaultReasoningEffort: null,
          defaultVisibleReasoningEffort: null,
          toolChoiceModes: ["auto"],
          structuredOutputStrategies: ["native_schema"],
          pricingStatus: "priced",
        },
      ],
    },
  ],
};

afterEach(() => {
  cleanup();
});

describe("ProviderModelMenu", () => {
  it("opens with publisher groups and selects an Anthropic model from catalog data", async () => {
    const onModelChange = vi.fn();
    render(
      <ProviderModelMenu
        catalog={catalog}
        selectedSelection={{ provider: "openai", model: "gpt-5-mini" }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    const anthropicPublisher = await screen.findByText("Anthropic");
    expect(await screen.findByText("OpenAI")).toBeTruthy();
    expect(screen.queryByText("Claude Sonnet 4.6")).toBeNull();

    const anthropicPublisherItem = anthropicPublisher.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(anthropicPublisherItem, { pointerType: "mouse" });
    fireEvent.pointerMove(anthropicPublisherItem, { pointerType: "mouse" });
    fireEvent.mouseMove(anthropicPublisherItem);

    const anthropicModel = await screen.findByText("Claude Sonnet 4.6");
    fireEvent.click(anthropicModel);

    await waitFor(() => {
      expect(onModelChange).toHaveBeenCalledWith({
        provider: "anthropic",
        model: "claude-sonnet-4-6",
      });
    });
  });

  it("passes reasoning effort only for models that expose visible effort options", async () => {
    const onModelChange = vi.fn();
    render(
      <ProviderModelMenu
        catalog={catalog}
        selectedSelection={{ provider: "openai", model: "gpt-5-mini" }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    const openAIPublisher = await screen.findByText("OpenAI");
    const openAIPublisherItem = openAIPublisher.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(openAIPublisherItem, { pointerType: "mouse" });
    fireEvent.pointerMove(openAIPublisherItem, { pointerType: "mouse" });
    fireEvent.mouseMove(openAIPublisherItem);

    let openAIModel: HTMLElement | undefined;
    await waitFor(() => {
      openAIModel = screen
        .getAllByText("GPT-5 mini")
        .find((element) => element.closest("[role='menuitem']")) as HTMLElement | undefined;
      expect(openAIModel).toBeTruthy();
    });
    const openAIModelItem = openAIModel.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(openAIModelItem, { pointerType: "mouse" });
    fireEvent.pointerMove(openAIModelItem, { pointerType: "mouse" });
    fireEvent.mouseMove(openAIModelItem);

    const highEffort = await screen.findByText("high");
    fireEvent.click(highEffort);

    await waitFor(() => {
      expect(onModelChange).toHaveBeenCalledWith(
        { provider: "openai", model: "gpt-5-mini" },
        { reasoningEffort: "high" },
      );
    });
  });

  it("opens reasoning directly for a single deployment-backed model", async () => {
    const onModelChange = vi.fn();
    const deploymentRef = {
      deployment_id: "11111111-1111-4111-8111-111111111111",
      expected_revision: 2,
    };
    const deploymentCatalog: LLMModelCatalogResponse = {
      providers: catalog.providers.map((provider) =>
        provider.id === "openai"
          ? {
              ...provider,
              models: provider.models.map((model) => ({
                ...model,
                deploymentRef,
                runnable: true,
              })),
            }
          : provider,
      ),
    };

    render(
      <ProviderModelMenu
        catalog={deploymentCatalog}
        selectedSelection={{ provider: "openai", model: "gpt-5-mini", deploymentRef }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    const openAIPublisher = await screen.findByText("OpenAI");
    const openAIPublisherItem = openAIPublisher.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(openAIPublisherItem, { pointerType: "mouse" });
    fireEvent.pointerMove(openAIPublisherItem, { pointerType: "mouse" });
    fireEvent.mouseMove(openAIPublisherItem);

    let openAIModelItem: HTMLElement | undefined;
    await waitFor(() => {
      openAIModelItem = screen
        .getAllByText("GPT-5 mini")
        .map((element) => element.closest("[role='menuitem']"))
        .find((element): element is HTMLElement => element instanceof HTMLElement);
      expect(openAIModelItem).toBeTruthy();
    });
    fireEvent.pointerEnter(openAIModelItem, { pointerType: "mouse" });
    fireEvent.pointerMove(openAIModelItem, { pointerType: "mouse" });
    fireEvent.mouseMove(openAIModelItem);

    fireEvent.click(await screen.findByText("high"));

    await waitFor(() => {
      expect(onModelChange).toHaveBeenCalledWith(
        { provider: "openai", model: "gpt-5-mini", deploymentRef },
        { reasoningEffort: "high" },
      );
    });
  });

  it("selects a single deployment-backed model with no reasoning directly", async () => {
    const onModelChange = vi.fn();
    const deploymentRef = {
      deployment_id: "22222222-2222-4222-8222-222222222222",
      expected_revision: 3,
    };
    const deploymentCatalog: LLMModelCatalogResponse = {
      providers: catalog.providers.map((provider) =>
        provider.id === "anthropic"
          ? {
              ...provider,
              defaultModel: "claude-haiku-4-5-20251001",
              models: provider.models.map((model) => ({
                ...model,
                id: "claude-haiku-4-5-20251001",
                label: "Claude Haiku 4.5",
                deploymentRef,
                runnable: true,
              })),
            }
          : provider,
      ),
    };

    render(
      <ProviderModelMenu
        catalog={deploymentCatalog}
        selectedSelection={{ provider: "openai", model: "gpt-5-mini" }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    const anthropicPublisher = await screen.findByText("Anthropic");
    const anthropicPublisherItem = anthropicPublisher.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(anthropicPublisherItem, { pointerType: "mouse" });
    fireEvent.pointerMove(anthropicPublisherItem, { pointerType: "mouse" });
    fireEvent.mouseMove(anthropicPublisherItem);

    fireEvent.click(await screen.findByText("Claude Haiku 4.5"));

    await waitFor(() => {
      expect(onModelChange).toHaveBeenCalledWith({
        provider: "anthropic",
        model: "claude-haiku-4-5-20251001",
        deploymentRef,
      });
    });
  });

  it("shows unavailable providers without allowing model selection", async () => {
    const onModelChange = vi.fn();
    const disabledCatalog: LLMModelCatalogResponse = {
      providers: [
        catalog.providers[0],
        {
          ...catalog.providers[1],
          available: false,
          selectable: false,
        },
      ],
    };

    render(
      <ProviderModelMenu
        catalog={disabledCatalog}
        selectedSelection={{ provider: "openai", model: "gpt-5-mini" }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    const anthropicPublisher = await screen.findByText("Anthropic");
    const anthropicPublisherItem = anthropicPublisher.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(anthropicPublisherItem, { pointerType: "mouse" });
    fireEvent.pointerMove(anthropicPublisherItem, { pointerType: "mouse" });
    fireEvent.mouseMove(anthropicPublisherItem);

    const anthropicRow = await screen.findByText("Claude Sonnet 4.6");
    expect(await screen.findByText("Unavailable")).toBeTruthy();
    fireEvent.click(anthropicRow);

    await waitFor(() => {
      expect(onModelChange).not.toHaveBeenCalled();
    });
  });

  it("requires enabled credentials for legacy model selection", async () => {
    const onModelChange = vi.fn();
    const credentialMissingCatalog: LLMModelCatalogResponse = {
      providers: catalog.providers.map((provider) =>
        provider.id === "openai"
          ? {
              ...provider,
              credential: {
                ...provider.credential,
                enabled: false,
                has_api_key: false,
                masked_api_key: null,
              },
            }
          : provider,
      ),
    };

    render(
      <ProviderModelMenu
        catalog={credentialMissingCatalog}
        selectedSelection={{ provider: "anthropic", model: "claude-sonnet-4-6" }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    const openAIPublisher = await screen.findByText("OpenAI");
    const openAIPublisherItem = openAIPublisher.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(openAIPublisherItem, { pointerType: "mouse" });
    fireEvent.pointerMove(openAIPublisherItem, { pointerType: "mouse" });
    fireEvent.mouseMove(openAIPublisherItem);

    const openAIModel = await screen.findByText("GPT-5 mini");
    expect(await screen.findByText("Configure credentials")).toBeTruthy();
    fireEvent.click(openAIModel);

    await waitFor(() => {
      expect(onModelChange).not.toHaveBeenCalled();
    });
  });

  it("groups GPT-OSS once and selects an explicit deployment ref", async () => {
    const onModelChange = vi.fn();
    const hfDeploymentRef = {
      deployment_id: "11111111-1111-4111-8111-111111111111",
      expected_revision: 2,
    };
    const nimDeploymentRef = {
      deployment_id: "22222222-2222-4222-8222-222222222222",
      expected_revision: 3,
    };
    const gptOssCatalog: LLMModelCatalogResponse = {
      providers: [
        {
          id: "huggingface_openai_compatible_chat",
          label: "Hugging Face",
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "huggingface_openai_compatible_chat",
            enabled: true,
            has_api_key: true,
          },
          defaultModel: "openai/gpt-oss-20b:fireworks-ai",
          models: [
            {
              id: "openai/gpt-oss-20b:fireworks-ai",
              canonicalModelId: "openai/gpt-oss-20b",
              exactWireModelId: "openai/gpt-oss-20b:fireworks-ai",
              label: "GPT-OSS 20B via Hugging Face",
              apiSurface: "chat_completions",
              capabilities: ["chat"],
              contextWindowTokens: 128000,
              maxOutputTokens: 8192,
              reasoningEfforts: [],
              visibleReasoningEfforts: [],
              defaultReasoningEffort: null,
              defaultVisibleReasoningEffort: null,
              toolChoiceModes: ["auto"],
              structuredOutputStrategies: [],
              pricingStatus: "unavailable",
              deploymentRef: hfDeploymentRef,
              runnable: true,
              connection: {
                presetId: "huggingface_openai_compatible_chat",
                displayName: "Hugging Face Router",
                enabled: true,
                authMode: "bearer_api_key",
                userConfigFields: ["api_key"],
                configFields: [],
                lifecycleState: "enabled",
                connectionRef: null,
                deploymentRef: hfDeploymentRef,
                verification: null,
                runnability: { status: "runnable", selectable: true, runnable: true },
              },
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
              id: "openai/gpt-oss-20b",
              canonicalModelId: "openai/gpt-oss-20b",
              exactWireModelId: "openai/gpt-oss-20b",
              label: "GPT-OSS 20B via NVIDIA NIM",
              apiSurface: "chat_completions",
              capabilities: ["chat"],
              contextWindowTokens: 128000,
              maxOutputTokens: 8192,
              reasoningEfforts: [],
              visibleReasoningEfforts: [],
              defaultReasoningEffort: null,
              defaultVisibleReasoningEffort: null,
              toolChoiceModes: ["auto"],
              structuredOutputStrategies: [],
              pricingStatus: "unavailable",
              deploymentRef: nimDeploymentRef,
              runnable: true,
              connection: {
                presetId: "nvidia_nim_openai_compatible_chat",
                displayName: "NVIDIA NIM Endpoint",
                enabled: true,
                authMode: "bearer_api_key",
                userConfigFields: ["api_key"],
                configFields: [],
                lifecycleState: "enabled",
                connectionRef: null,
                deploymentRef: nimDeploymentRef,
                verification: null,
                runnability: { status: "runnable", selectable: true, runnable: true },
              },
            },
          ],
        },
      ],
    };

    render(
      <ProviderModelMenu
        catalog={gptOssCatalog}
        selectedSelection={{
          provider: "openai",
          model: "gpt-oss-20b",
          deploymentRef: hfDeploymentRef,
        }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    expect(screen.getByRole("button", { name: "Select model" }).textContent).toContain(
      "GPT-OSS 20B / Hugging Face",
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    const openAIPublisher = await screen.findByText("Open models");
    expect(screen.queryByText("Hugging Face")).toBeNull();
    expect(screen.queryByText("NVIDIA NIM")).toBeNull();

    const openAIPublisherItem = openAIPublisher.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(openAIPublisherItem, { pointerType: "mouse" });
    fireEvent.pointerMove(openAIPublisherItem, { pointerType: "mouse" });
    fireEvent.mouseMove(openAIPublisherItem);

    const gptOssRows = await screen.findAllByText("GPT-OSS 20B");
    expect(gptOssRows).toHaveLength(1);
    expect(screen.queryByText("Hugging Face")).toBeNull();
    expect(screen.queryByText("NVIDIA NIM")).toBeNull();

    const modelItem = gptOssRows[0].closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(modelItem, { pointerType: "mouse" });
    fireEvent.pointerMove(modelItem, { pointerType: "mouse" });
    fireEvent.mouseMove(modelItem);

    fireEvent.click(await screen.findByText("NVIDIA"));

    await waitFor(() => {
      expect(onModelChange).toHaveBeenCalledWith({
        provider: "nvidia_nim_openai_compatible_chat",
        model: "openai/gpt-oss-20b",
        deploymentRef: nimDeploymentRef,
      });
    });
  });

  it("hides connection setup placeholders with no explicit deployment ref", async () => {
    const onModelChange = vi.fn();
    const placeholderCatalog: LLMModelCatalogResponse = {
      providers: [
        ...catalog.providers,
        ...[
          ["custom_openai_compatible_chat", "Custom OpenAI-compatible", "Custom OpenAI-compatible HTTPS endpoint"],
          ["huggingface_openai_compatible_chat", "Hugging Face", "Hugging Face Router"],
          ["nvidia_nim_openai_compatible_chat", "NVIDIA NIM", "NVIDIA NIM hosted endpoint"],
          ["ollama_openai_compatible_chat", "Ollama", "Ollama endpoint"],
          ["vllm_openai_compatible_chat", "vLLM", "vLLM endpoint"],
        ].map(([id, providerLabel, modelLabel]) => ({
          id,
          label: providerLabel,
          capabilities: [],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: id,
            enabled: false,
            has_api_key: false,
          },
          defaultModel: id,
          models: [
            {
              id,
              label: modelLabel,
              apiSurface: "chat_completions",
              capabilities: ["chat"],
              contextWindowTokens: 128000,
              maxOutputTokens: 8192,
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
                presetId: id,
                displayName: modelLabel,
                enabled: true,
                authMode: "bearer_api_key",
                userConfigFields: ["api_key"],
                configFields: [],
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
            },
          ],
        })),
      ],
    };

    render(
      <ProviderModelMenu
        catalog={placeholderCatalog}
        selectedSelection={{ provider: "openai", model: "gpt-5-mini" }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    expect((await screen.findAllByText("GPT-5 mini")).length).toBeGreaterThan(0);
    for (const clutter of [
      "Custom OpenAI-compatible HTTPS endpoint",
      "Hugging Face Router",
      "NVIDIA NIM hosted endpoint",
      "Ollama endpoint",
      "vLLM endpoint",
    ]) {
      expect(screen.queryByText(clutter)).toBeNull();
    }
  });
});
