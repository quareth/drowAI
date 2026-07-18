/**
 * Guards the single user-global deployment-selection mutation from chat controls.
 */
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const selectorsSource = readFileSync(
  new URL("../TaskModelSelectors.tsx", import.meta.url),
  "utf8",
);
const chatSource = readFileSync(
  new URL("../UnifiedAgentChat.tsx", import.meta.url),
  "utf8",
);

describe("TaskModelSelectors deployment selection", () => {
  it("delegates one model selection callback to its owner", () => {
    expect(selectorsSource.match(/onModelChange=\{onModelChange\}/g)).toHaveLength(1);
  });

  it("performs only the canonical user-global selection mutation", () => {
    expect(chatSource.match(/updateSelection\.mutate\(selection\)/g)).toHaveLength(1);
    expect(chatSource).not.toContain("switchTaskModel");
    expect(chatSource).not.toContain("/api/llm/tasks/${taskIdentifier}/switch");
  });
});
