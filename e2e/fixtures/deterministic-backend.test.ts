/** Regression tests for deterministic stack failure cleanup and process ownership. */

import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { allocateSuiteResources, cleanupSuiteResources } from "./suite-resources";
import { runMigrations, startDeterministicSuiteStack } from "./deterministic-backend";

test("startup failure terminates the process tree and removes only suite resources", async () => {
  const callerRoot = await mkdtemp(join(tmpdir(), "drowai-e2e-startup-failure-"));
  const sentinelPath = join(callerRoot, "caller-owned.txt");
  const pidPath = join(callerRoot, "backend.pid");
  const descendantPidPath = join(callerRoot, "descendant.pid");
  await writeFile(sentinelPath, "preserve", "utf8");

  try {
    await assert.rejects(
      startDeterministicSuiteStack({
        args: [
          "-c",
          [
            "from pathlib import Path",
            "import os",
            "import signal",
            "import subprocess",
            "import sys",
            "import time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "descendant = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])",
            "signal.signal(signal.SIGTERM, signal.SIG_DFL)",
            "Path(os.environ['E2E_TEST_PID_FILE']).write_text(str(os.getpid()))",
            "Path(os.environ['E2E_TEST_DESCENDANT_PID_FILE']).write_text(str(descendant.pid))",
            "time.sleep(30)",
          ].join("; "),
        ],
        extraEnv: {
          E2E_TEST_PID_FILE: pidPath,
          E2E_TEST_DESCENDANT_PID_FILE: descendantPidPath,
        },
        startupDelayMs: 100,
        resources: { baseDir: callerRoot, label: "expected-failure" },
      }),
      /did not become ready/i,
    );

    assert.equal(await readFile(sentinelPath, "utf8"), "preserve");
    const entries = await readdir(callerRoot);
    assert.equal(entries.some((entry) => entry.startsWith("drowai-e2e-suite-")), false);

    const childPid = Number(await readFile(pidPath, "utf8"));
    const descendantPid = Number(await readFile(descendantPidPath, "utf8"));
    assertProcessIsGone(childPid);
    assertProcessIsGone(descendantPid);
  } finally {
    await rm(callerRoot, { recursive: true, force: true });
  }
});

test("migration output is suite-owned and sanitized before mirroring", async () => {
  const root = await mkdtemp(join(tmpdir(), "drowai-e2e-migration-log-"));
  const command = join(root, "fake-python");
  const logRoot = join(root, "logs");
  await mkdir(logRoot, { recursive: true });
  await writeFile(
    command,
    "#!/bin/sh\nprintf '%s\\n' 'Authorization: Basic migration-secret' 'authorization=opaque-migration-secret' >&2\n",
    "utf8",
  );
  await chmod(command, 0o700);

  try {
    const logPath = join(logRoot, "migrations.log");
    await runMigrations(command, process.cwd(), process.env, logPath);
    const log = await readFile(logPath, "utf8");
    assert.equal(log.includes("migration-secret"), false);
    assert.equal(log.includes("opaque-migration-secret"), false);
    assert.match(log, /Authorization: <REDACTED>/);
    assert.match(log, /authorization=<REDACTED>/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

function assertProcessIsGone(pid: number): void {
  assert.throws(
    () => process.kill(pid, 0),
    (error: NodeJS.ErrnoException) => error.code === "ESRCH",
  );
}
