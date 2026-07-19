/**
 * Characterizes the limited provider settings surface.
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
  it("loads provider settings from the public model catalog only", () => {
    expect(settingsSource).toContain("fetchLLMModelCatalog");
    expect(settingsSource).not.toContain("fetchReportingLLMSelection");
    expect(settingsSource).not.toContain("saveReportingLLMSelection");
    expect(settingsSource).toContain('from "@/features/llm-provider/api"');
    expect(settingsSource).toContain("queryKey: catalogQueryKey");
  });

  it("renders credential cards only for direct providers", () => {
    expect(settingsSource).toContain("credentialProviders.map((provider)");
    expect(settingsSource).toContain("<ProviderCredentialCard");
    expect(settingsSource).toContain("provider={provider}");
    expect(settingsSource).toContain("key={provider.id}");
  });

  it("keeps reporting and deployment selection controls out of settings", () => {
    expect(settingsSource).not.toContain("Reporting model");
    expect(settingsSource).not.toContain("Workload deployment");
    expect(settingsSource).not.toContain("Advanced model preferences");
    expect(settingsSource).not.toContain("Capability evidence");
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
