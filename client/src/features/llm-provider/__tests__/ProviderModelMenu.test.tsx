// @vitest-environment jsdom
/**
 * Verifies provider-first model picker behavior from backend catalog data.
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
  it("opens with provider rows and selects an Anthropic model from catalog data", async () => {
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

    expect(await screen.findByText("OpenAI")).toBeTruthy();
    const anthropicRow = await screen.findByText("Anthropic");
    expect(anthropicRow).toBeTruthy();
    const anthropicMenuItem = anthropicRow.closest("[role='menuitem']") as HTMLElement;

    fireEvent.pointerEnter(anthropicMenuItem, { pointerType: "mouse" });
    fireEvent.pointerMove(anthropicMenuItem, { pointerType: "mouse" });
    fireEvent.mouseMove(anthropicMenuItem);

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
    const openAIRow = await screen.findByText("OpenAI");
    const openAIMenuItem = openAIRow.closest("[role='menuitem']") as HTMLElement;
    fireEvent.pointerEnter(openAIMenuItem, { pointerType: "mouse" });
    fireEvent.pointerMove(openAIMenuItem, { pointerType: "mouse" });
    fireEvent.mouseMove(openAIMenuItem);

    const openAIModel = await screen.findByText("GPT-5 mini");
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

    const anthropicRow = await screen.findByText("Anthropic");
    expect(await screen.findByText("Unavailable")).toBeTruthy();
    const anthropicMenuItem = anthropicRow.closest("[role='menuitem']") as HTMLElement;

    fireEvent.pointerEnter(anthropicMenuItem, { pointerType: "mouse" });
    fireEvent.pointerMove(anthropicMenuItem, { pointerType: "mouse" });
    fireEvent.mouseMove(anthropicMenuItem);

    const anthropicModel = await screen.findByText("Claude Sonnet 4.6");
    fireEvent.click(anthropicModel);

    await waitFor(() => {
      expect(onModelChange).not.toHaveBeenCalled();
    });
  });
});
