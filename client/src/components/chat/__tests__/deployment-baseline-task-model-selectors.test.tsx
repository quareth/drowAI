/**
 * Characterizes chat header provider/model selection and usage display.
 */
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const taskModelSelectorsSource = readFileSync(
  new URL("../TaskModelSelectors.tsx", import.meta.url),
  "utf8",
);
const providerModelMenuSource = readFileSync(
  new URL("../../../features/llm-provider/ProviderModelMenu.tsx", import.meta.url),
  "utf8",
);

describe("deployment baseline TaskModelSelectors", () => {
  it("delegates provider/model selection to ProviderModelMenu with catalog data", () => {
    expect(taskModelSelectorsSource).toContain("ProviderModelMenu");
    expect(taskModelSelectorsSource).toContain("catalog={llmCatalog}");
    expect(taskModelSelectorsSource).toContain("selectedSelection={selectedSelection}");
    expect(taskModelSelectorsSource).toContain("onModelChange={onModelChange}");
  });

  it("keeps pricing status out of the chat model menu during Phase 0", () => {
    expect(providerModelMenuSource).not.toContain("model.pricingStatus");
    expect(providerModelMenuSource).not.toContain("Pricing: ${normalized}");
    expect(providerModelMenuSource).not.toContain("{pricingStatus}");
  });

  it("keeps usage fetching on the task usage endpoint in the chat header", () => {
    expect(taskModelSelectorsSource).toContain("apiCall(`/api/tasks/${selectedTaskId}/usage`)");
    expect(taskModelSelectorsSource).toContain("formatCostUSD(usageData.cost_usd)");
    expect(taskModelSelectorsSource).toContain("formatTokenCount(usageData.prompt_tokens)");
    expect(taskModelSelectorsSource).toContain("formatTokenCount(usageData.completion_tokens)");
  });
});
