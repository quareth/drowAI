/** Sanitizes and captures backend/frontend output for E2E failure diagnostics. */

import type { ChildProcess } from "node:child_process";
import { createWriteStream, mkdirSync, type WriteStream } from "node:fs";
import { dirname } from "node:path";
import type { Writable } from "node:stream";

const REDACTED = "<REDACTED>";

export interface SanitizedLogCapture {
  finished: Promise<void>;
}

/** Remove common credential-bearing header and key/value formats from logs. */
export function sanitizeServerLog(value: string): string {
  return value
    .replace(/(\bauthorization\s*["']?\s*[:=]\s*)[^\r\n]*/gi, `$1${REDACTED}`)
    .replace(
      /((?:set-)?cookie\s*["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\r\n]+)/gi,
      `$1${REDACTED}`,
    )
    .replace(
      /((?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|token|jwt|secret)\s*["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)/gi,
      `$1${REDACTED}`,
    )
    .replace(
      /((["'])token\2\s*:\s*)("[^"]*"|'[^']*'|[^\s,;}]+)/gi,
      `$1${REDACTED}`,
    );
}

/** Capture a child process's stdout/stderr after line-buffered sanitization. */
export async function captureSanitizedProcessLogs(
  child: ChildProcess,
  logPath: string,
  mirror: Writable | null = process.stdout,
): Promise<SanitizedLogCapture> {
  mkdirSync(dirname(logPath), { recursive: true });
  const output = createWriteStream(logPath, { flags: "a", mode: 0o600 });
  const finished = new Promise<void>((resolve, reject) => {
    output.once("finish", resolve);
    output.once("error", reject);
  });
  attachStream(child.stdout, output, mirror);
  attachStream(child.stderr, output, mirror);
  child.once("close", () => output.end());
  return { finished };
}

function attachStream(
  input: NodeJS.ReadableStream | null,
  output: WriteStream,
  mirror: Writable | null,
): void {
  if (!input) {
    return;
  }
  let pending = "";
  input.setEncoding("utf8");
  input.on("data", (chunk: string) => {
    pending += chunk;
    const lines = pending.split(/(?<=\n)/);
    pending = lines.pop() ?? "";
    for (const line of lines) {
      writeSanitized(line, output, mirror);
    }
  });
  input.on("end", () => {
    if (pending) {
      writeSanitized(pending, output, mirror);
    }
  });
}

function writeSanitized(value: string, output: WriteStream, mirror: Writable | null): void {
  const sanitized = sanitizeServerLog(value);
  output.write(sanitized);
  mirror?.write(sanitized);
}
