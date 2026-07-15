/** Verifies real failing authenticated Playwright artifacts contain no credentials. */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

test("failing authenticated artifacts exclude bearer, cookie, and password secrets", async () => {
  const contractRoot = await mkdtemp(join(tmpdir(), "drowai-artifact-policy-"));
  const outputRoot = join(contractRoot, "playwright");
  const htmlReportRoot = join(contractRoot, "playwright-report");
  const server = createServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end("<!doctype html><title>artifact probe</title>");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert(address && typeof address !== "string");

  const secrets = {
    token: "artifact-probe-bearer-token-9f90",
    cookie: "artifact-probe-cookie-6a31",
    password: "artifact-probe-password-2d77",
  };
  try {
    const result = await runPlaywright(
      "npx",
      [
        "playwright",
        "test",
        "--config=e2e/probes/playwright.config.ts",
        "--project=chromium",
        `--output=${outputRoot}`,
      ],
      {
        cwd: process.cwd(),
        env: {
          ...process.env,
          CI: "true",
          PLAYWRIGHT_HTML_OUTPUT_DIR: htmlReportRoot,
          E2E_ARTIFACT_POLICY_PROBE: "true",
          E2E_ARTIFACT_PROBE_URL: `http://127.0.0.1:${address.port}`,
          E2E_ARTIFACT_PROBE_TOKEN: secrets.token,
          E2E_ARTIFACT_PROBE_COOKIE: secrets.cookie,
          E2E_ARTIFACT_PROBE_PASSWORD: secrets.password,
        },
      },
    );
    assert.equal(result.status, 1, result.stderr || result.stdout);

    const files = (
      await Promise.all([outputRoot, htmlReportRoot].map(collectFiles))
    ).flat();
    assert(files.length > 0, "The intentional failure must retain at least one artifact.");
    for (const file of files) {
      const contents = await readFile(file);
      for (const secret of Object.values(secrets)) {
        assert.equal(contents.includes(Buffer.from(secret)), false, `${file} leaked ${secret}`);
      }
    }
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
    await rm(contractRoot, { recursive: true, force: true });
  }
});

async function runPlaywright(
  command: string,
  args: string[],
  options: { cwd: string; env: NodeJS.ProcessEnv },
): Promise<{ status: number | null; stdout: string; stderr: string }> {
  const child = spawn(command, args, { ...options, stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => (stdout += chunk));
  child.stderr.on("data", (chunk: string) => (stderr += chunk));
  const status = await new Promise<number | null>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  });
  return { status, stdout, stderr };
}

async function collectFiles(root: string): Promise<string[]> {
  const entries = await readdir(root, { withFileTypes: true }).catch(() => []);
  const files = await Promise.all(
    entries.map((entry) => {
      const path = join(root, entry.name);
      return entry.isDirectory() ? collectFiles(path) : [path];
    }),
  );
  return files.flat();
}
