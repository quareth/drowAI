// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { normalizeTranscriptItemsToSteps, type ChatTranscriptItem } from "@/hooks/chat-history-bootstrap";

describe("normalizeTranscriptItemsToSteps", () => {
  it("maps reasoning transcript items to start + delta + section_end lifecycle", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "r-1",
        kind: "reasoning",
        turn_number: 10,
        content: "think",
        metadata: {
          sequence_authority: "legacy_reasoning_blob",
          timestamp: "2026-03-01T12:00:08Z",
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(99, items);
    const stepTypes = steps.map((step) => (step.metadata as Record<string, unknown>)?.step_type);
    expect(stepTypes).toEqual([
      "reasoning_start",
      "reasoning_delta",
      "reasoning_section_end",
    ]);
    expect((steps[0].metadata as Record<string, unknown>)?.sequence_authority).toBe(
      "legacy_reasoning_blob",
    );
    expect(steps[0].timestamp).toBeUndefined();
    expect(steps[1].timestamp).toBe("2026-03-01T12:00:08Z");
    expect(steps[2].timestamp).toBeUndefined();
  });

  it("uses persisted reasoning boundary timestamps for replayed lifecycle steps", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "r-2",
        kind: "reasoning",
        turn_number: 11,
        content: "planned",
        metadata: {
          sequence_authority: "canonical_detail",
          timestamp: "2026-03-01T12:00:30Z",
          started_at: 1_709_294_400,
          ended_at: 1_709_294_408.4,
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(99, items);
    expect(steps[0].timestamp).toBe("2024-03-01T12:00:00.000Z");
    expect(steps[1].timestamp).toBe("2024-03-01T12:00:00.000Z");
    expect(steps[2].timestamp).toBe("2024-03-01T12:00:08.400Z");
  });

  it("maps observation transcript items to delta + section_end", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "o-1",
        kind: "observation",
        turn_number: 10,
        content: "observe",
        metadata: {},
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(99, items);
    const stepTypes = steps.map((step) => (step.metadata as Record<string, unknown>)?.step_type);
    expect(stepTypes).toEqual([
      "observation_delta",
      "observation_section_end",
    ]);
  });

  it("maps tool transcript items to tool_start + tool_end with status and tool_call_id", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "tool-fallback-id",
        kind: "tool",
        turn_number: 11,
        content: "tool output",
        metadata: {
          tool_call_id: "call-123",
          tool_name: "bash",
          status: "error",
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(99, items);
    expect(steps).toHaveLength(2);

    const startMeta = steps[0].metadata as Record<string, unknown>;
    const endMeta = steps[1].metadata as Record<string, unknown>;
    expect(startMeta.step_type).toBe("tool_start");
    expect(endMeta.step_type).toBe("tool_end");
    expect(startMeta.tool_call_id).toBe("call-123");
    expect(endMeta.tool_call_id).toBe("call-123");
    expect(endMeta.status).toBe("error");
  });

  it("preserves tool_batch_id on replayed tool lifecycle steps", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "tool-80",
        kind: "tool",
        turn_number: 12,
        content: "port 80 closed",
        metadata: {
          tool_call_id: "call-80",
          tool_batch_id: "tb-web",
          tool_name: "nmap",
          status: "success",
        },
      },
      {
        id: "tool-443",
        kind: "tool",
        turn_number: 12,
        content: "port 443 open",
        metadata: {
          tool_call_id: "call-443",
          tool_batch_id: "tb-web",
          tool_name: "nmap",
          status: "success",
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(99, items);
    const toolLifecycleSteps = steps.filter((step) => {
      const stepType = (step.metadata as Record<string, unknown>)?.step_type;
      return stepType === "tool_start" || stepType === "tool_end";
    });

    expect(toolLifecycleSteps).toHaveLength(4);
    expect(
      toolLifecycleSteps.every(
        (step) => (step.metadata as Record<string, unknown>)?.tool_batch_id === "tb-web",
      ),
    ).toBe(true);
  });

  it("preserves retryable assistant metadata for refresh-safe retry rendering", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "assistant-err",
        kind: "assistant",
        turn_number: 12,
        content: "[Error] Retry me",
        metadata: {
          status: "error",
          retryable: true,
          retry_mode: "checkpoint",
          error_code: "provider_structured_output_parse",
          error_message: "Retry from checkpoint",
          graph_name: "simple_tool",
          turn_id: "task-12-turn-12",
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(99, items);
    expect(steps).toHaveLength(1);

    const metadata = steps[0].metadata as Record<string, unknown>;
    expect(metadata.status).toBe("error");
    expect(metadata.retryable).toBe(true);
    expect(metadata.retry_mode).toBe("checkpoint");
    expect(metadata.error_code).toBe("provider_structured_output_parse");
    expect(metadata.graph_name).toBe("simple_tool");
    expect(metadata.turn_id).toBe("task-12-turn-12");
  });

  it("preserves provider refusal metadata for refresh-safe declined rendering", () => {
    const refusal = {
      provider: "anthropic",
      model: "claude-fable-5",
      category: "cyber",
      summary: "The provider declined this request under its cyber safety policy.",
      explanation: "Blocked by policy.",
      response_id: "msg_123",
      partial: true,
    };
    const items: ChatTranscriptItem[] = [
      {
        id: "assistant-refusal",
        kind: "assistant",
        turn_number: 13,
        content: "Partial answer",
        metadata: {
          status: "declined",
          stop_reason: "refusal",
          outcome_type: "provider_refusal",
          retryable: false,
          refusal,
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(99, items);
    const metadata = steps[0].metadata as Record<string, unknown>;
    expect(metadata.status).toBe("declined");
    expect(metadata.stop_reason).toBe("refusal");
    expect(metadata.outcome_type).toBe("provider_refusal");
    expect(metadata.retryable).toBe(false);
    expect(metadata.refusal).toEqual(refusal);
  });

  it("preserves replay sequence and sub-turn index metadata for deterministic ordering", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "obs-0",
        kind: "observation",
        turn_number: 42,
        content: "first observation",
        metadata: {
          sequence: 42001,
          sub_turn_index: 0,
          timestamp: "2026-03-01T12:00:00Z",
        },
      },
      {
        id: "tool-0",
        kind: "tool",
        turn_number: 42,
        content: "tool output",
        metadata: {
          sequence: 42000,
          tool_call_id: "call-1",
          tool_name: "nmap",
          sub_turn_index: 0,
          timestamp: "2026-03-01T12:00:00Z",
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(77, items);
    const observationDelta = steps.find(
      (step) => (step.metadata as Record<string, unknown>)?.step_type === "observation_delta",
    );
    const toolStart = steps.find(
      (step) => (step.metadata as Record<string, unknown>)?.step_type === "tool_start",
    );
    expect(observationDelta).toBeDefined();
    expect(toolStart).toBeDefined();
    expect((observationDelta?.metadata as Record<string, unknown>)?.sequence).toBe(42001);
    expect((observationDelta?.metadata as Record<string, unknown>)?.sub_turn_index).toBe(0);
    expect((toolStart?.metadata as Record<string, unknown>)?.sequence).toBe(42000);
    expect((toolStart?.metadata as Record<string, unknown>)?.sub_turn_index).toBe(0);
  });

  it("preserves alternating tool/observation cycles for one turn via canonical sequence metadata", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "tool-1",
        kind: "tool",
        turn_number: 70,
        content: "tool one output",
        metadata: {
          sequence: 7001,
          tool_call_id: "call-1",
          tool_name: "nmap",
          sub_turn_index: 0,
        },
      },
      {
        id: "obs-1",
        kind: "observation",
        turn_number: 70,
        content: "observation one",
        metadata: {
          sequence: 7002,
          sub_turn_index: 0,
        },
      },
      {
        id: "tool-2",
        kind: "tool",
        turn_number: 70,
        content: "tool two output",
        metadata: {
          sequence: 7003,
          tool_call_id: "call-2",
          tool_name: "curl",
          sub_turn_index: 1,
        },
      },
      {
        id: "obs-2",
        kind: "observation",
        turn_number: 70,
        content: "observation two",
        metadata: {
          sequence: 7004,
          sub_turn_index: 1,
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(77, items);
    const cycleStarts = steps.filter((step) => {
      const stepType = (step.metadata as Record<string, unknown>)?.step_type;
      return stepType === "tool_start" || stepType === "observation_delta";
    });

    expect(cycleStarts.map((step) => (step.metadata as Record<string, unknown>)?.step_type)).toEqual([
      "tool_start",
      "observation_delta",
      "tool_start",
      "observation_delta",
    ]);
    expect(cycleStarts.map((step) => (step.metadata as Record<string, unknown>)?.sequence)).toEqual([
      7001,
      7002,
      7003,
      7004,
    ]);
  });

  it("keeps canonical sequence when backend sends 0-based phase sequence", () => {
    const items: ChatTranscriptItem[] = [
      {
        id: "obs-zero",
        kind: "observation",
        turn_number: 88,
        content: "first canonical observation",
        metadata: {
          sequence: 0,
          sub_turn_index: 0,
        },
      },
    ];

    const steps = normalizeTranscriptItemsToSteps(77, items);
    const observationDelta = steps.find(
      (step) => (step.metadata as Record<string, unknown>)?.step_type === "observation_delta",
    );
    expect(observationDelta).toBeDefined();
    expect(observationDelta?.sequence).toBe(0);
    expect((observationDelta?.metadata as Record<string, unknown>)?.sequence).toBe(0);
  });
});
