/** Contract tests for credential-safe E2E server log capture. */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { captureSanitizedProcessLogs, sanitizeServerLog } from "./sanitized-logs";

test("sanitizes every authorization scheme plus cookie, password, and token material", () => {
  const raw = [
    "Authorization: Bearer secret.jwt.value",
    "Authorization: Basic dXNlcjpzZWNyZXQ=",
    "AUTHORIZATION: ApiKey private-api-key",
    "authorization=opaque-secret",
    "Cookie: session=private-cookie",
    "Set-Cookie: access_token=private-token; HttpOnly",
    `{'cookie': 'session=private-structured-cookie'}`,
    `{"set-cookie":"session=private-structured-set-cookie"}`,
    'password="private-password"',
    '"api_key":"private-api-key"',
    'access_token="private-access-token"',
    "token=private-plain-token",
    '{"email":"owner@example.test","token":"private-actor-token"}',
    "Token count: 42",
  ].join("\n");

  const sanitized = sanitizeServerLog(raw);

  for (const secret of [
    "secret.jwt.value",
    "dXNlcjpzZWNyZXQ=",
    "opaque-secret",
    "private-cookie",
    "private-token",
    "private-structured-cookie",
    "private-structured-set-cookie",
    "private-password",
    "private-api-key",
    "private-access-token",
    "private-plain-token",
    "private-actor-token",
  ]) {
    assert.equal(sanitized.includes(secret), false);
  }
  assert.match(sanitized, /<REDACTED>/);
  assert.match(sanitized, /Authorization: <REDACTED>/);
  assert.match(sanitized, /AUTHORIZATION: <REDACTED>/);
  assert.match(sanitized, /authorization=<REDACTED>/);
  assert.match(sanitized, /Token count: 42/);
});

test("captures child output to a sanitized file", async () => {
  const root = await mkdtemp(join(tmpdir(), "drowai-e2e-log-contract-"));
  const logPath = join(root, "server.log");
  const child = spawn(
    process.execPath,
    [
      "-e",
      "console.log('Authorization: Bearer child-process-secret'); " +
        `console.log(JSON.stringify({cookie: 'session=child-process-cookie'}))`,
    ],
    { stdio: ["ignore", "pipe", "pipe"] },
  );

  try {
    const capture = await captureSanitizedProcessLogs(child, logPath, null);
    await capture.finished;
    const log = await readFile(logPath, "utf8");
    assert.equal(log.includes("child-process-secret"), false);
    assert.equal(log.includes("child-process-cookie"), false);
    assert.match(log, /Authorization: <REDACTED>/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
