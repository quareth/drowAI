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
  it("opens with model rows and selects an Anthropic model from catalog data", async () => {
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

    const anthropicModel = await screen.findByText("Claude Sonnet 4.6");
    expect(screen.queryByText("OpenAI")).toBeNull();
    expect(screen.queryByText("Anthropic")).toBeNull();
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
    const openAIModel = (await screen.findAllByText("GPT-5 mini")).find((element) =>
      element.closest("[role='menuitem']"),
    ) as HTMLElement;
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

    const anthropicRow = await screen.findByText("Claude Sonnet 4.6");
    expect(await screen.findByText("Unavailable")).toBeTruthy();
    fireEvent.click(anthropicRow);

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
          provider: "huggingface_openai_compatible_chat",
          model: "openai/gpt-oss-20b:fireworks-ai",
        }}
        selectedReasoningEffort="medium"
        onModelChange={onModelChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));

    const gptOssRows = await screen.findAllByText("GPT-OSS 20B");
    expect(gptOssRows).toHaveLength(1);
    expect(screen.queryByText("Hugging Face")).toBeNull();
    expect(screen.queryByText("NVIDIA NIM")).toBeNull();

    const modelItem = gptOssRows[0].closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(modelItem, { pointerType: "mouse" });
    fireEvent.pointerMove(modelItem, { pointerType: "mouse" });
    fireEvent.mouseMove(modelItem);

    fireEvent.click(await screen.findByText("NVIDIA NIM"));

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
