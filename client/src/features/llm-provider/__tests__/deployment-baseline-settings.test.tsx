/**
 * Characterizes current provider settings catalog, credential, and reporting flows.
 */
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const settingsSource = readFileSync(
  new URL("../ProviderSettingsSection.tsx", import.meta.url),
  "utf8",
);
const credentialCardSource = readFileSync(
  new URL("../ProviderCredentialCard.tsx", import.meta.url),
  "utf8",
);

describe("deployment baseline provider settings", () => {
  it("uses llm-provider api helpers for catalog and reporting selection", () => {
    expect(settingsSource).toContain("fetchLLMModelCatalog");
    expect(settingsSource).toContain("fetchReportingLLMSelection");
    expect(settingsSource).toContain("saveReportingLLMSelection");
    expect(settingsSource).toContain('from "@/features/llm-provider/api"');
    expect(settingsSource).toContain("queryKey: catalogQueryKey");
    expect(settingsSource).toContain("queryKey: reportingSelectionQueryKey");
  });

  it("renders provider credential cards from backend catalog providers", () => {
    expect(settingsSource).toContain("providers.map((provider)");
    expect(settingsSource).toContain("<ProviderCredentialCard");
    expect(settingsSource).toContain("provider={provider}");
    expect(settingsSource).toContain("key={provider.id}");
  });

  it("saves reporting model selection with backend provider, model, and reasoning payload", () => {
    expect(settingsSource).toContain("saveReportingSelection.mutate({");
    expect(settingsSource).toContain("provider: selection.provider");
    expect(settingsSource).toContain("model: selection.model");
    expect(settingsSource).toContain("reasoning_effort: reasoningEffort ?? null");
  });

  it("keeps provider credential mutations behind llm-provider api helpers", () => {
    expect(credentialCardSource).toContain("saveLLMProviderCredential");
    expect(credentialCardSource).toContain("testLLMProviderCredential");
    expect(credentialCardSource).toContain("deleteLLMProviderCredential");
    expect(credentialCardSource).toContain('from "@/features/llm-provider/api"');
    expect(credentialCardSource).toContain("api_key: trimmedKey");
    expect(credentialCardSource).toContain("enabled: true");
  });
});
