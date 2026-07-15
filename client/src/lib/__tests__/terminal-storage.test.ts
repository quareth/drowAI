// @vitest-environment jsdom
/**
 * Verifies centralized terminal dock sessionStorage behavior.
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  clearTaskTerminalStorage,
  getTerminalBuffer,
  getTerminalSessionId,
  readTerminalList,
  removeTerminalFromList,
  setTerminalBuffer,
  setTerminalSessionId,
  writeTerminalList,
} from "@/lib/terminal-storage";

describe("terminal-storage", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it("deduplicates terminal records by task and preserves one active item", () => {
    writeTerminalList([
      { id: "term-a", taskId: 7, taskName: "Old", isActive: false },
      { id: "term-b", taskId: 7, taskName: "New", isActive: true },
      { id: "term-c", taskId: 8, taskName: "Other", isActive: false },
    ]);

    expect(readTerminalList()).toEqual([
      { id: "term-b", taskId: 7, taskName: "New", isActive: true },
      { id: "term-c", taskId: 8, taskName: "Other", isActive: false },
    ]);
  });

  it("removes a terminal from the persisted list synchronously", () => {
    writeTerminalList([
      { id: "term-a", taskId: 7, taskName: "A", isActive: true },
      { id: "term-b", taskId: 8, taskName: "B", isActive: false },
    ]);

    const next = removeTerminalFromList("term-a");

    expect(next).toEqual([{ id: "term-b", taskId: 8, taskName: "B", isActive: true }]);
    expect(JSON.parse(sessionStorage.getItem("term:list") || "[]")).toEqual(next);
  });

  it("clears task-local terminal session id and buffer keys together", () => {
    setTerminalSessionId(7, "sid-7");
    setTerminalBuffer(7, "output");

    clearTaskTerminalStorage(7);

    expect(getTerminalSessionId(7)).toBeNull();
    expect(getTerminalBuffer(7)).toBe("");
  });
});
