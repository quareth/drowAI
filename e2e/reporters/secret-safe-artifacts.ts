/** Sanitizes failure diagnostics before other reporters or CI retain them. */

import { readFile, readdir, rm, writeFile } from "node:fs/promises";
import { extname, join } from "node:path";
import type {
  FullConfig,
  Reporter,
  TestCase,
  TestError,
  TestResult,
  TestStep,
} from "@playwright/test/reporter";

import { sanitizeServerLog } from "../fixtures/sanitized-logs";

const TEXT_ARTIFACT_EXTENSIONS = new Set([".html", ".json", ".log", ".md", ".txt"]);

interface SecretSafeArtifactReporterOptions {
  additionalOutputRoots?: string[];
}

export default class SecretSafeArtifactReporter implements Reporter {
  private outputRoots = new Set<string>();

  constructor(private readonly options: SecretSafeArtifactReporterOptions = {}) {}

  onBegin(config: FullConfig): void {
    for (const project of config.projects) {
      this.outputRoots.add(project.outputDir);
    }
    for (const root of this.options.additionalOutputRoots ?? []) {
      this.outputRoots.add(root);
    }
  }

  async onTestEnd(_test: TestCase, result: TestResult): Promise<void> {
    sanitizeTestResult(result);
    await Promise.all([...this.outputRoots].map(sanitizeFailureArtifacts));
  }

  async onEnd(): Promise<void> {
    await Promise.all([...this.outputRoots].map(sanitizeFailureArtifacts));
  }

  async onExit(): Promise<void> {
    // Playwright guarantees every reporter has completed onEnd before onExit,
    // so the HTML report exists before this final retained-artifact pass.
    await Promise.all([...this.outputRoots].map(sanitizeFailureArtifacts));
  }
}

/** Redact diagnostics before downstream reporters serialize them into HTML. */
export function sanitizeTestResult(result: TestResult): void {
  result.stdout = result.stdout.map(sanitizeOutputChunk);
  result.stderr = result.stderr.map(sanitizeOutputChunk);
  result.errors.forEach(sanitizeTestError);
  if (result.error) {
    sanitizeTestError(result.error);
  }
  sanitizeAttachments(result.attachments);
  result.steps.forEach(sanitizeTestStep);
}

/** Remove unsafe traces and redact credential-shaped text from retained artifacts. */
export async function sanitizeFailureArtifacts(root: string): Promise<void> {
  const entries = await readdir(root, { withFileTypes: true }).catch(() => []);
  await Promise.all(
    entries.map(async (entry) => {
      const path = join(root, entry.name);
      if (entry.isDirectory()) {
        await sanitizeFailureArtifacts(path);
        return;
      }
      if (entry.name === "trace.zip") {
        await rm(path, { force: true });
        return;
      }
      if (!TEXT_ARTIFACT_EXTENSIONS.has(extname(entry.name).toLowerCase())) {
        return;
      }
      const original = await readFile(path, "utf8");
      const sanitized = sanitizeArtifactText(original);
      if (sanitized !== original) {
        await writeFile(path, sanitized, { encoding: "utf8", mode: 0o600 });
      }
    }),
  );
}

/** Redact server-log credentials and accessibility snapshots of password fields. */
export function sanitizeArtifactText(value: string): string {
  return sanitizeServerLog(value).replace(
    /^(\s*-\s*(?:password\s+)?textbox(?:\s+[^:\r\n]+)?\s*:\s*).+$/gim,
    "$1<REDACTED>",
  );
}

function sanitizeTestError(error: TestError): void {
  for (const field of ["message", "snippet", "stack", "value"] as const) {
    if (error[field]) {
      error[field] = sanitizeArtifactText(error[field]);
    }
  }
  if (error.cause) {
    sanitizeTestError(error.cause);
  }
}

function sanitizeTestStep(step: TestStep): void {
  if (step.error) {
    sanitizeTestError(step.error);
  }
  sanitizeAttachments(step.attachments);
  step.steps.forEach(sanitizeTestStep);
}

function sanitizeAttachments(
  attachments: Array<{ name: string; contentType: string; path?: string; body?: Buffer }>,
): void {
  for (const attachment of attachments) {
    if (attachment.name === "trace" || attachment.path?.endsWith("trace.zip")) {
      attachment.path = undefined;
      attachment.body = undefined;
      continue;
    }
    if (attachment.body && isTextContentType(attachment.contentType)) {
      attachment.body = Buffer.from(sanitizeArtifactText(attachment.body.toString("utf8")));
    }
  }
}

function sanitizeOutputChunk(chunk: string | Buffer): string | Buffer {
  return Buffer.isBuffer(chunk)
    ? Buffer.from(sanitizeArtifactText(chunk.toString("utf8")))
    : sanitizeArtifactText(chunk);
}

function isTextContentType(contentType: string): boolean {
  return contentType.startsWith("text/") || /(?:json|xml|javascript)/i.test(contentType);
}
