/** Contract tests for sanitizing credential-bearing Playwright diagnostics. */

import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  sanitizeArtifactText,
  sanitizeFailureArtifacts,
  sanitizeTestResult,
} from "../reporters/secret-safe-artifacts";
import type { TestResult } from "@playwright/test/reporter";

test("redacts password textbox snapshots and structured credentials", () => {
  const sanitized = sanitizeArtifactText(
    [
      "  - textbox [ref=e132]: private-password",
      `{"cookie":"session=private-cookie"}`,
      "Authorization: Bearer private-token",
    ].join("\n"),
  );

  for (const secret of ["private-password", "private-cookie", "private-token"]) {
    assert.equal(sanitized.includes(secret), false);
  }
});

test("redacts serialized actor tokens without corrupting token-related prose", () => {
  const sanitized = sanitizeArtifactText(
    [
      JSON.stringify({
        email: "owner@example.test",
        token: "private-actor-token",
        tenantId: "tenant-1",
      }),
      "Token count: 42",
      "The token bucket remains healthy.",
    ].join("\n"),
  );

  assert.equal(sanitized.includes("private-actor-token"), false);
  assert.match(sanitized, /"token":<REDACTED>/);
  assert.match(sanitized, /Token count: 42/);
  assert.match(sanitized, /The token bucket remains healthy\./);
});

test("deletes trace archives and sanitizes retained text artifacts", async () => {
  const root = await mkdtemp(join(tmpdir(), "drowai-artifact-sanitize-"));
  const contextPath = join(root, "error-context.md");
  const tracePath = join(root, "trace.zip");
  try {
    await writeFile(contextPath, "- textbox: private-password");
    await writeFile(tracePath, "private-token");
    await sanitizeFailureArtifacts(root);

    assert.equal((await readFile(contextPath, "utf8")).includes("private-password"), false);
    await assert.rejects(readFile(tracePath), { code: "ENOENT" });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("redacts in-memory diagnostics before the HTML reporter serializes them", () => {
  const result = {
    stdout: ["Authorization: Bearer private-token"],
    stderr: [Buffer.from("password: private-password")],
    errors: [{ message: "cookie: private-cookie", stack: "secret: private-secret" }],
    error: { message: "cookie: private-cookie" },
    attachments: [
      {
        name: "request",
        contentType: "application/json",
        body: Buffer.from('{"token":"private-attachment-token"}'),
      },
    ],
    steps: [],
  } as unknown as TestResult;

  sanitizeTestResult(result);

  const retained = JSON.stringify(result, (_key, value) =>
    Buffer.isBuffer(value) ? value.toString("utf8") : value,
  );
  for (const secret of [
    "private-token",
    "private-password",
    "private-cookie",
    "private-secret",
    "private-attachment-token",
  ]) {
    assert.equal(retained.includes(secret), false);
  }
});
