/**
 * Verifies UnifiedAgentChat uses the canonical global model mutation path.
 */
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const source = readFileSync(
  new URL("../UnifiedAgentChat.tsx", import.meta.url),
  "utf8",
);

describe("UnifiedAgentChat deployment baseline", () => {
  it("uses llm-provider api helpers for catalog and global selection", () => {
    expect(source).toContain("fetchLLMModelCatalog");
    expect(source).toContain("fetchLLMSelection");
    expect(source).toContain("saveLLMSelection");
    expect(source).toContain('from "@/features/llm-provider/api"');
  });

  it("uses one user-global selection mutation without a task-switch request", () => {
    expect(source).toContain("updateSelection.mutate(selection)");
    expect(source).not.toContain("switchTaskModel");
    expect(source).not.toContain("/api/llm/tasks/${taskIdentifier}/switch");
  });
});
