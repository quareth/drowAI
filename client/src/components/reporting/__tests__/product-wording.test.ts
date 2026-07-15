/* Verifies reporting frontend artifacts use product-facing terminology. */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const repositoryRoot = fileURLToPath(new URL("../../../../..", import.meta.url));

const reportingArtifactPaths = [
  "client/src/components/reporting",
  "client/src/hooks/use-reporting.ts",
  "client/src/pages/reports-page.tsx",
  "client/src/components/panels/task-panel.tsx",
  "client/src/components/panels/task-panel-card.tsx",
] as const;

const sourceExtensions = new Set([".ts", ".tsx"]);
const forbiddenProductLabelTokens = ["wa" + "ve"];

function sourceFilesIn(path: string): string[] {
  const absolutePath = resolve(repositoryRoot, path);
  const stats = statSync(absolutePath);

  if (stats.isFile()) {
    return [absolutePath];
  }

  return readdirSync(absolutePath)
    .flatMap((entry) => sourceFilesIn(join(absolutePath, entry)))
    .filter((filePath) =>
      [...sourceExtensions].some((extension) => filePath.endsWith(extension)),
    );
}

function productWordingViolations(): string[] {
  const forbiddenPatterns = forbiddenProductLabelTokens.map(
    (token) => new RegExp(`\\b${token}\\b`, "i"),
  );

  return reportingArtifactPaths
    .flatMap(sourceFilesIn)
    .flatMap((filePath) => {
      const lines = readFileSync(filePath, "utf-8").split(/\r?\n/);
      return lines.flatMap((line, index) =>
        forbiddenPatterns.some((pattern) => pattern.test(line))
          ? [`${relative(repositoryRoot, filePath)}:${index + 1}`]
          : [],
      );
    });
}

describe("reporting product wording", () => {
  it("keeps frontend reporting artifacts on product terminology", () => {
    expect(productWordingViolations()).toEqual([]);
  });
});
