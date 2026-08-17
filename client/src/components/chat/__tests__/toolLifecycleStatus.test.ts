/** Tests for canonical tool and shell lifecycle presentation precedence. */

import { describe, expect, it } from "vitest";

import {
  deriveShellLifecycleLabels,
  deriveToolLifecycleStatus,
} from "@/components/chat/toolLifecycleStatus";

describe("tool lifecycle status", () => {
  it.each([
    ["success", "running", "executing"],
    ["error", "completed", "completed"],
    ["success", "timed_out", "timed_out"],
    ["success", "terminated", "terminated"],
    ["success", "failed", "failed"],
  ] as const)(
    "gives process status precedence over %s invocation status",
    (rawStatus, processStatus, expected) => {
      expect(deriveToolLifecycleStatus(rawStatus, processStatus)).toBe(expected);
    },
  );

  it("falls back to generic tool status when process status is absent", () => {
    expect(deriveToolLifecycleStatus("cancel_requested", undefined)).toBe("cancelled");
  });

  it("keeps terminal process labels ahead of closed-session labels", () => {
    expect(
      deriveShellLifecycleLabels({
        processStatus: "timed_out",
        sessionStatus: "closed",
        interactionBoundary: "terminal",
      }),
    ).toEqual({ processLabel: "Process timed out", activityLabel: "" });
  });

  it("describes active output without changing process precedence", () => {
    expect(
      deriveShellLifecycleLabels({
        processStatus: "running",
        sessionStatus: "active",
        interactionBoundary: "output_available",
      }),
    ).toEqual({
      processLabel: "Session active",
      activityLabel: "Agent reviewing output",
    });
  });
});
