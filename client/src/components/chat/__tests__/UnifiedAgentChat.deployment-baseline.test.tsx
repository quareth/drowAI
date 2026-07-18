/**
 * Characterizes UnifiedAgentChat's current duplicate model mutation path.
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

  it("documents the residual duplicate global-save and task-switch mutation", () => {
    expect(source).toContain("updateSelection.mutate(selection)");
    expect(source).toContain("switchTaskModel.mutate({ taskId: activeTaskId, ...selection })");
    expect(source).toContain("!featureFlags.enableBasicChat");
    expect(source).toContain('apiRequest("POST", `/api/llm/tasks/${taskIdentifier}/switch`');
  });
});
