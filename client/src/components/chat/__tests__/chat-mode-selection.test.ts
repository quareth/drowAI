/**
 * Phase 6 composite chat-mode selection tests.
 *
 * Pin the invariants that make Plan a route overlay rather than a
 * primary mode: legacy ``ChatExperienceMode`` hydration, mutual
 * exclusivity of Chat and Plan, and the transport-shape payload that
 * the backend boundary reads.
 */
import { describe, expect, it } from "vitest";

import {
  chatExperienceModeToComposite,
  chatSelectionToAgentModePayload,
  compositeToChatExperienceMode,
  type ChatModeSelection,
} from "../types";

describe("chatExperienceModeToComposite", () => {
  it("hydrates legacy 'plan' into (agent, plan=true)", () => {
    // Phase 6 Task 6.9: migration must not lose the user's selection.
    expect(chatExperienceModeToComposite("plan")).toEqual({
      primary: "agent",
      plan: true,
    });
  });

  it("hydrates 'chat' into (chat, plan=false)", () => {
    expect(chatExperienceModeToComposite("chat")).toEqual({
      primary: "chat",
      plan: false,
    });
  });

  it("hydrates 'agent' into (agent, plan=false)", () => {
    expect(chatExperienceModeToComposite("agent")).toEqual({
      primary: "agent",
      plan: false,
    });
  });

  it("hydrates 'agent_full' into (agent_full, plan=false)", () => {
    expect(chatExperienceModeToComposite("agent_full")).toEqual({
      primary: "agent_full",
      plan: false,
    });
  });

  it("hydrates 'agent_full_plan' into (agent_full, plan=true)", () => {
    expect(chatExperienceModeToComposite("agent_full_plan")).toEqual({
      primary: "agent_full",
      plan: true,
    });
  });
});

describe("compositeToChatExperienceMode", () => {
  it("round-trips (chat, plan=false) -> 'chat'", () => {
    expect(compositeToChatExperienceMode({ primary: "chat", plan: false })).toBe(
      "chat",
    );
  });

  it("collapses (agent, plan=true) -> 'plan' for legacy persistence", () => {
    expect(compositeToChatExperienceMode({ primary: "agent", plan: true })).toBe(
      "plan",
    );
  });

  it("round-trips (agent_full, plan=false) -> 'agent_full'", () => {
    expect(
      compositeToChatExperienceMode({ primary: "agent_full", plan: false }),
    ).toBe("agent_full");
  });

  it("round-trips (agent_full, plan=true) -> 'agent_full_plan' (lossless)", () => {
    // Full Access + Plan must be distinguishable from Agent + Plan on
    // the legacy round-trip so the autonomy tier survives hydration.
    expect(
      compositeToChatExperienceMode({ primary: "agent_full", plan: true }),
    ).toBe("agent_full_plan");
  });

  it("forces 'chat' regardless of plan flag to enforce mutual exclusivity", () => {
    // ``chat + plan`` is not representable on the wire; the converter
    // must collapse to ``chat`` so persisted legacy state never
    // carries an invalid combination.
    expect(compositeToChatExperienceMode({ primary: "chat", plan: true })).toBe(
      "chat",
    );
  });
});

describe("chatSelectionToAgentModePayload", () => {
  it.each<{ selection: ChatModeSelection; agent_mode: string; plan_mode: boolean }>([
    { selection: { primary: "agent", plan: false }, agent_mode: "agent", plan_mode: false },
    { selection: { primary: "agent", plan: true }, agent_mode: "agent", plan_mode: true },
    { selection: { primary: "agent_full", plan: false }, agent_mode: "full_access", plan_mode: false },
    { selection: { primary: "agent_full", plan: true }, agent_mode: "full_access", plan_mode: true },
    { selection: { primary: "chat", plan: false }, agent_mode: "chat", plan_mode: false },
  ])(
    "maps $selection to agent_mode=$agent_mode plan_mode=$plan_mode",
    ({ selection, agent_mode, plan_mode }) => {
      expect(chatSelectionToAgentModePayload(selection)).toEqual({
        agent_mode,
        plan_mode,
      });
    },
  );

  it("never emits the legacy 'plan' agent_mode", () => {
    // Phase 6 Task 6.7/6.9: the new UI never sends ``agent_mode=plan``.
    const all: ChatModeSelection[] = [
      { primary: "chat", plan: false },
      { primary: "agent", plan: false },
      { primary: "agent", plan: true },
      { primary: "agent_full", plan: false },
      { primary: "agent_full", plan: true },
    ];
    for (const selection of all) {
      const payload = chatSelectionToAgentModePayload(selection);
      expect(payload.agent_mode).not.toBe("plan");
      expect(["chat", "agent", "full_access"]).toContain(payload.agent_mode);
    }
  });

  it("forces plan_mode=false when primary is chat", () => {
    // Chat + Plan is mutually exclusive — the payload builder
    // defensively collapses the plan flag in case a caller bypasses
    // the dropdown's own disable logic.
    expect(
      chatSelectionToAgentModePayload({ primary: "chat", plan: true }),
    ).toEqual({ agent_mode: "chat", plan_mode: false });
  });
});
