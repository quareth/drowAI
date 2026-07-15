import { afterEach, describe, expect, it } from "vitest";

import {
  getWorkbenchStateSnapshot,
  openTerminalForTask,
  resetWorkbenchState,
  setTerminalCollapsed,
  toggleTerminalCollapsed,
} from "@/state/workbench-state-store";

afterEach(() => {
  resetWorkbenchState();
});

describe("workbench-state-store", () => {
  it("opens terminal dock and records focus request for a task", () => {
    openTerminalForTask(42);

    const snapshot = getWorkbenchStateSnapshot();
    expect(snapshot.isTerminalCollapsed).toBe(false);
    expect(snapshot.terminalTaskId).toBe(42);
    expect(snapshot.terminalRequestNonce).toBe(1);
  });

  it("increments request nonce even when focusing the same task", () => {
    openTerminalForTask(7);
    openTerminalForTask(7);

    const snapshot = getWorkbenchStateSnapshot();
    expect(snapshot.terminalTaskId).toBe(7);
    expect(snapshot.terminalRequestNonce).toBe(2);
  });

  it("supports explicit and toggle collapse controls", () => {
    openTerminalForTask(9);
    setTerminalCollapsed(true);
    expect(getWorkbenchStateSnapshot().isTerminalCollapsed).toBe(true);

    toggleTerminalCollapsed();
    expect(getWorkbenchStateSnapshot().isTerminalCollapsed).toBe(false);
  });
});
