import { describe, it, expect } from 'vitest';

import type { Step } from '@/utils/reasoning-normalizer';
import { stepToChatMessage } from '@/utils/stepToChatMessage';

/** Helper to build a minimal Step with the given overrides. */
function buildStep(overrides: Partial<Step> = {}): Step {
  return {
    type: 'tool_end',
    content: '',
    ...overrides,
  };
}

describe('stepToChatMessage', () => {
  // ------------------------------------------------------------------ //
  //  Metadata preservation (regression guard for Issue 11 / Issue 12)   //
  // ------------------------------------------------------------------ //

  it('preserves summary dict in metadata (tool output)', () => {
    const summary = {
      observation: 'Host 10.0.0.1 is up (0.0012s latency).',
      stdout_excerpt: 'Starting Nmap 7.94 ...',
      stderr_excerpt: '',
    };
    const step = buildStep({
      metadata: { step_type: 'tool_end', summary, tool_call_id: 'call_abc' },
    });

    const msg = stepToChatMessage(step);

    expect(msg.metadata).toBeDefined();
    expect((msg.metadata as Record<string, unknown>).summary).toEqual(summary);
  });

  it('preserves compact_tool_result in metadata (compact tool output)', () => {
    const compactToolResult = {
      schema_version: '2.0',
      tool: 'nmap',
      status: 'success',
      success: true,
      exit_code: 0,
      summary: 'Nmap scan completed.',
      key_findings: ['Port 80 open'],
      errors: [],
      report_recommendations: ['Review exposed service'],
      structured_signals: [{ type: 'service', port: 80, service: 'http' }],
      decision_evidence: ['80/tcp open http'],
      lossiness_risk: 'low',
    };
    const step = buildStep({
      metadata: { step_type: 'tool_end', compact_tool_result: compactToolResult, tool_call_id: 'call_compact' },
    });

    const msg = stepToChatMessage(step);

    expect((msg.metadata as Record<string, unknown>).compact_tool_result).toEqual(compactToolResult);
  });

  it('preserves status field in metadata', () => {
    const step = buildStep({
      metadata: { step_type: 'tool_end', status: 'success', tool_call_id: 'call_1' },
    });

    const msg = stepToChatMessage(step);

    expect((msg.metadata as Record<string, unknown>).status).toBe('success');
  });

  it('preserves tool_call_id for downstream grouping', () => {
    const step = buildStep({
      metadata: { step_type: 'tool_start', tool_call_id: 'call_xyz789' },
    });

    const msg = stepToChatMessage(step);

    expect((msg.metadata as Record<string, unknown>).tool_call_id).toBe('call_xyz789');
  });

  it('preserves step_type in metadata', () => {
    const step = buildStep({
      metadata: { step_type: 'tool_end' },
    });

    const msg = stepToChatMessage(step);

    expect((msg.metadata as Record<string, unknown>).step_type).toBe('tool_end');
  });

  it('preserves conversationId in metadata', () => {
    const step = buildStep({
      metadata: { step_type: 'assistant_message', conversationId: 'conv-42' },
    });

    const msg = stepToChatMessage(step);

    expect((msg.metadata as Record<string, unknown>).conversationId).toBe('conv-42');
  });

  it('preserves all extra metadata fields without filtering', () => {
    const step = buildStep({
      metadata: {
        step_type: 'tool_end',
        tool_call_id: 'call_extra',
        status: 'success',
        parameters: { target: '10.0.0.1' },
        duration: 1234,
        subtype: 'nmap',
        source: 'executor',
        ind: 1,
        turn_sequence: 5,
        summary: { observation: 'done' },
      },
    });

    const msg = stepToChatMessage(step);
    const meta = msg.metadata as Record<string, unknown>;

    expect(meta.parameters).toEqual({ target: '10.0.0.1' });
    expect(meta.duration).toBe(1234);
    expect(meta.subtype).toBe('nmap');
    expect(meta.source).toBe('executor');
    expect(meta.ind).toBe(1);
    expect(meta.turn_sequence).toBe(5);
  });

  // ------------------------------------------------------------------ //
  //  Message type resolution                                            //
  // ------------------------------------------------------------------ //

  it('resolves "executing" type for tool_end step_type', () => {
    const step = buildStep({
      metadata: { step_type: 'tool_end' },
    });

    expect(stepToChatMessage(step).type).toBe('executing');
  });

  it('resolves "executing" type for tool_start step_type', () => {
    const step = buildStep({
      metadata: { step_type: 'tool_start' },
    });

    expect(stepToChatMessage(step).type).toBe('executing');
  });

  it('resolves "thinking" type for reasoning_delta step_type', () => {
    const step = buildStep({
      type: 'reasoning_delta',
      metadata: { step_type: 'reasoning_delta' },
    });

    expect(stepToChatMessage(step).type).toBe('thinking');
  });

  it('resolves "user" type when metadata.role is "user"', () => {
    const step = buildStep({
      type: 'user_message',
      metadata: { role: 'user', step_type: 'user_message' },
    });

    expect(stepToChatMessage(step).type).toBe('user');
  });

  it('resolves "system" type when metadata.role is "system"', () => {
    const step = buildStep({
      type: 'status',
      metadata: { role: 'system' },
    });

    expect(stepToChatMessage(step).type).toBe('system');
  });

  it('resolves "agent" when no step_type, role, or classifiable type is present', () => {
    const step = buildStep({ type: 'assistant_message', metadata: {} });

    expect(stepToChatMessage(step).type).toBe('agent');
  });

  // ------------------------------------------------------------------ //
  //  ID resolution                                                      //
  // ------------------------------------------------------------------ //

  it('uses __internalKey when present', () => {
    const step = buildStep({ __internalKey: 'internal-key-42' });

    expect(stepToChatMessage(step).id).toBe('internal-key-42');
  });

  it('uses client_message_id from metadata', () => {
    const step = buildStep({
      metadata: { client_message_id: 'client-msg-99' },
    });

    expect(stepToChatMessage(step).id).toBe('client-msg-99');
  });

  it('falls back to sequence-based id', () => {
    const step = buildStep({
      metadata: { turn_sequence: 7 },
    });

    expect(stepToChatMessage(step).id).toBe('seq-7');
  });

  // ------------------------------------------------------------------ //
  //  Content & streaming                                                //
  // ------------------------------------------------------------------ //

  it('passes through string content', () => {
    const step = buildStep({ content: 'Nmap scan complete.' });

    expect(stepToChatMessage(step).content).toBe('Nmap scan complete.');
  });

  it('defaults to empty string for non-string content', () => {
    const step = buildStep({ content: undefined as unknown as string });

    expect(stepToChatMessage(step).content).toBe('');
  });

  it('maps isStreaming from Step', () => {
    const streaming = buildStep({ isStreaming: true });
    const done = buildStep({ isStreaming: false });

    expect(stepToChatMessage(streaming).isStreaming).toBe(true);
    expect(stepToChatMessage(done).isStreaming).toBe(false);
  });
});
