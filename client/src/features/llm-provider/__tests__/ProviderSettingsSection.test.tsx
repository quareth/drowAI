// @vitest-environment jsdom
/**
 * Verifies provider-neutral LLM credential settings flows.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ProviderCredentialCard from "../ProviderCredentialCard";
import ProviderSettingsSection from "../ProviderSettingsSection";
import type { LLMCatalogProvider, LLMModelCatalogResponse } from "../types";

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

const openAIProvider: LLMCatalogProvider = {
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
  defaultModel: "gpt-5.2",
  models: [
    {
      id: "gpt-5.2",
      label: "GPT-5.2",
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
};

const anthropicProvider: LLMCatalogProvider = {
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
};

const catalog: LLMModelCatalogResponse = {
  providers: [openAIProvider, anthropicProvider],
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
  mocked.fetchLLMSelection.mockResolvedValue({
    provider: "openai",
    model: "gpt-5.2",
    deploymentRef: null,
    selectionStatus: { status: "selectable", selectable: true, runnable: true },
  });
  mocked.fetchReportingLLMSelection.mockResolvedValue({
    provider: "openai",
    model: "gpt-5.2",
    reasoningEffort: "medium",
    selectionStatus: { runnable: true, reason: null },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProviderSettingsSection", () => {
  it("shows a catalog load error instead of the empty-provider state", async () => {
    mocked.fetchLLMModelCatalog.mockRejectedValue(new Error("Catalog request failed"));

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    expect(await screen.findByText("Unable to load LLM providers")).toBeTruthy();
    expect(screen.getByText("Catalog request failed")).toBeTruthy();
    expect(screen.queryByText("No providers available")).toBeNull();
  });

  it("renders reporting selection above provider credentials without internal controls", async () => {
    mocked.fetchLLMModelCatalog.mockResolvedValue(catalog);

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Reporting model" })).toBeTruthy();
    const sectionHeadings = screen.getAllByRole("heading", { level: 3 });
    expect(sectionHeadings[0].textContent).toBe("Reporting model");
    expect(sectionHeadings[1].textContent).toBe("AI providers");
    expect(screen.getByRole("button", { name: "Select model" })).toBeTruthy();
    expect(await screen.findByText("OpenAI")).toBeTruthy();
    expect(screen.getAllByText("OpenAI").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Anthropic").length).toBeGreaterThan(0);
    expect(screen.queryByText("Workload deployment")).toBeNull();
    expect(screen.queryByRole("button", { name: "Advanced model preferences" })).toBeNull();
    expect(screen.queryByText(/capability evidence|lifecycle|runnability/i)).toBeNull();
    expect(screen.queryByRole("combobox", { name: /selected provider/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /save model selection/i })).toBeNull();
  });

  it("persists a reporting model selected from the top control", async () => {
    mocked.fetchLLMModelCatalog.mockResolvedValue({
      providers: catalog.providers.map((provider) =>
        provider.id === "anthropic"
          ? {
              ...provider,
              credential: {
                ...provider.credential,
                enabled: true,
                has_api_key: true,
              },
            }
          : provider,
      ),
    });
    mocked.saveReportingLLMSelection.mockResolvedValue({
      provider: "anthropic",
      model: "claude-sonnet-4-6",
      reasoningEffort: null,
      selectionStatus: { runnable: true, reason: null },
    });

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    fireEvent.pointerDown(await screen.findByRole("button", { name: "Select model" }));
    const anthropicMenuItem = screen
      .getAllByText("Anthropic")
      .find((element) => element.closest("[role='menuitem']"))
      ?.closest("[role='menuitem']") as HTMLElement;
    expect(anthropicMenuItem).toBeTruthy();
    fireEvent.pointerEnter(anthropicMenuItem, { pointerType: "mouse" });
    fireEvent.pointerMove(anthropicMenuItem, { pointerType: "mouse" });
    fireEvent.mouseMove(anthropicMenuItem);
    fireEvent.click(await screen.findByText("Claude Sonnet 4.6"));

    await waitFor(() => {
      expect(mocked.saveReportingLLMSelection).toHaveBeenCalledWith({
        provider: "anthropic",
        model: "claude-sonnet-4-6",
        reasoning_effort: null,
      });
    });
  });
});

describe("ProviderCredentialCard", () => {
  it("connects, verifies, and disconnects provider credentials without using legacy OpenAI settings", async () => {
    mocked.saveLLMProviderCredential.mockResolvedValue(openAIProvider.credential);
    mocked.testLLMProviderCredential.mockResolvedValue({
      provider: "openai",
      status: "ok",
      message: "ok",
      model_count: 3,
    });
    mocked.deleteLLMProviderCredential.mockResolvedValue({ success: true });

    renderWithQueryClient(
      <ProviderCredentialCard
        provider={openAIProvider}
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    const providerCard = screen.getByRole("group", { name: "OpenAI provider settings" });
    expect(within(providerCard).getByLabelText("OpenAI status: Connected")).toBeTruthy();
    expect(within(providerCard).getByText("Stored key:").textContent).toContain("sk-...1234");
    expect(within(providerCard).getByRole("button", { name: "Show API key" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("API Key"), {
      target: { value: "sk-test-value" },
    });
    fireEvent.click(screen.getByRole("button", { name: /update openai/i }));

    await waitFor(() => {
      expect(mocked.saveLLMProviderCredential).toHaveBeenCalledWith("openai", {
        api_key: "sk-test-value",
        enabled: true,
      });
      expect(mocked.testLLMProviderCredential).toHaveBeenCalledWith("openai", {
        api_key: "sk-test-value",
      });
    });

    fireEvent.click(screen.getByRole("button", { name: /disconnect openai/i }));
    await waitFor(() => {
      expect(mocked.deleteLLMProviderCredential).toHaveBeenCalledWith("openai");
    });
  });
});
