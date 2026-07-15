import type { ChatMessage } from '@/components/chat/types';
import { describe, it, expect } from 'vitest';

import { groupMessages } from '@/hooks/useMessageGrouping';
import { STEP_COMPARATOR } from '@/utils/reasoning-normalizer';
import type { Step } from '@/utils/reasoning-normalizer';

function buildMessage(
  overrides: Partial<ChatMessage> & { id: string; metadata: Record<string, unknown> },
): ChatMessage {
  return {
    type: overrides.type ?? 'agent',
    content: overrides.content ?? '',
    timestamp: overrides.timestamp ?? new Date().toISOString(),
    isStreaming: overrides.isStreaming ?? false,
    ...overrides,
  };
}

describe('groupMessages', () => {
  it('orders groups by turn sequence and semantic phase (reasoning -> tool -> observation -> message)', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'final',
        content: 'done',
        metadata: { step_type: 'assistant_message', ind: 2, turn_sequence: '10' },
        timestamp: '2024-01-01T00:00:03Z',
      }),
      buildMessage({
        id: 'tool',
        content: 'running tool',
        metadata: { step_type: 'tool_delta', ind: 1, turn_sequence: '10' },
        timestamp: '2024-01-01T00:00:02Z',
      }),
      buildMessage({
        id: 'thinking',
        content: 'reasoning chunk',
        metadata: {
          step_type: 'reasoning_delta',
          ind: 0,
          turn_sequence: '10',
          reasoning_section_id: 'turn-10:reasoning:0',
          phase_sequence: 0,
        },
        timestamp: '2024-01-01T00:00:01Z',
      }),
      buildMessage({
        id: 'next-turn-thinking',
        content: 'next reasoning',
        metadata: {
          step_type: 'reasoning_delta',
          ind: 0,
          turn_sequence: '11',
          reasoning_section_id: 'turn-11:reasoning:0',
          phase_sequence: 0,
        },
        timestamp: '2024-01-01T00:00:04Z',
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups.map((group) => group.primaryType)).toEqual([
      'reasoning',
      'tool',
      'message',
      'reasoning',
    ]);
    expect(groups[0].messages[0].id).toBe('thinking');
    expect(groups[2].messages[0].id).toBe('final');
    expect(groups[3].messages[0].id).toBe('next-turn-thinking');
  });

  it('orders groups by canonical sequence within a turn, not semantic phase rank', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'obs',
        content: 'obs delta',
        metadata: {
          step_type: 'observation_delta',
          ind: 1,
          turn_sequence: 55,
          sequence: 2000,
          id: 'obs-1',
        },
      }),
      buildMessage({
        id: 'tool',
        content: 'tool output',
        metadata: {
          step_type: 'tool_start',
          tool_call_id: 'tc-55',
          ind: 1,
          turn_sequence: 55,
          sequence: 2001,
          id: 'tool-55',
        },
      }),
      buildMessage({
        id: 'reasoning',
        content: 'reasoning delta',
        metadata: {
          step_type: 'reasoning_start',
          ind: 1,
          turn_sequence: 55,
          sequence: 2002,
          id: 'reason-55',
          reasoning_section_id: 'turn-55:reasoning:2',
          phase_sequence: 2,
        },
      }),
    ];

    const groups = groupMessages(messages);

    // Canonical sequence (2000 < 2001 < 2002) wins over semantic phase rank
    expect(groups.map((group) => group.primaryType)).toEqual(['observation', 'tool', 'reasoning']);
    expect(groups[0].messages[0].id).toBe('obs');
    expect(groups[1].messages[0].id).toBe('tool');
    expect(groups[2].messages[0].id).toBe('reasoning');
  });

  it('falls back to `sequence` when turn sequence is missing', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'delta',
        metadata: {
          step_type: 'reasoning_delta',
          sequence: '200',
          ind: 0,
          reasoning_section_id: 'turn-200:reasoning:0',
          phase_sequence: 0,
        },
        timestamp: '2024-01-01T00:00:01Z',
      }),
      buildMessage({
        id: 'answer',
        metadata: { step_type: 'assistant_message', sequence: '200', ind: 2 },
        timestamp: '2024-01-01T00:00:02Z',
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups.map((group) => group.primaryType)).toEqual(['reasoning', 'message']);
  });

  it('merges multiple message-phase events for the same turn into a single group', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'stream-start',
        content: '',
        metadata: { step_type: 'message_start', ind: 2, turn_sequence: '42', id: 'lg-1' },
        timestamp: '2024-01-01T00:00:01Z',
      }),
      buildMessage({
        id: 'stream-delta',
        content: 'partial',
        metadata: { step_type: 'message_delta', ind: 2, turn_sequence: '42', id: 'lg-1' },
        timestamp: '2024-01-01T00:00:02Z',
      }),
      // Persisted assistant_message snapshot with different id but same turn_sequence
      buildMessage({
        id: 'snapshot',
        content: 'partial',
        metadata: { step_type: 'assistant_message', ind: 2, turn_sequence: '42', id: 'lg-1-snapshot' },
        timestamp: '2024-01-01T00:00:03Z',
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups).toHaveLength(1);
    expect(groups[0].primaryType).toBe('message');
    expect(groups[0].messages.map((m) => m.id)).toEqual([
      'stream-start',
      'stream-delta',
      'snapshot',
    ]);
  });

  it('groups tool_start, tool_delta, and tool_end into a single tool group by tool_call_id', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'start',
        content: 'Executing nmap...',
        metadata: { step_type: 'tool_start', tool_call_id: 'call_abc', ind: 1, turn_sequence: '10', tool: 'nmap' },
      }),
      buildMessage({
        id: 'delta',
        content: 'scan output',
        metadata: { step_type: 'tool_delta', tool_call_id: 'call_abc', ind: 1, turn_sequence: '10', tool: 'nmap' },
      }),
      buildMessage({
        id: 'end',
        content: 'Tool nmap completed (success)',
        metadata: {
          step_type: 'tool_end',
          tool_call_id: 'call_abc',
          ind: 1,
          turn_sequence: '10',
          tool: 'nmap',
          status: 'success',
          summary: { observation: 'Host 10.0.0.1 is up' },
        },
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups).toHaveLength(1);
    expect(groups[0].primaryType).toBe('tool');
    expect(groups[0].messages).toHaveLength(3);
  });

  it('resolves tool status as "completed" even when tool_start appears after tool_end (Issue 11 regression)', () => {
    // After page refresh, replay events may be sorted so tool_start comes
    // after tool_end (alphabetical __internalKey tiebreaker). The renderer
    // must still show "completed" because tool_end is present in the group.
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'delta',
        content: 'scan output',
        metadata: { step_type: 'tool_delta', tool_call_id: 'call_abc', ind: 1, tool: 'nmap' },
      }),
      buildMessage({
        id: 'end',
        content: 'Tool nmap completed (success)',
        metadata: {
          step_type: 'tool_end',
          tool_call_id: 'call_abc',
          ind: 1,
          tool: 'nmap',
          status: 'success',
          summary: { observation: 'Host 10.0.0.1 is up' },
        },
      }),
      // tool_start arrives LAST (alphabetical sort: 's' > 'e' > 'd')
      buildMessage({
        id: 'start',
        content: 'Executing nmap...',
        metadata: { step_type: 'tool_start', tool_call_id: 'call_abc', ind: 1, tool: 'nmap' },
      }),
    ];

    const groups = groupMessages(messages);
    const toolGroup = groups.find((g) => g.primaryType === 'tool');
    expect(toolGroup).toBeDefined();

    // Simulate what MessageGroupRenderer does: iterate and resolve status
    let status: 'executing' | 'completed' | 'failed' = 'executing';
    for (const msg of toolGroup!.messages) {
      const stepType = (msg.metadata as Record<string, unknown>)?.step_type;
      if (stepType === 'tool_end') {
        const statusValue = ((msg.metadata as Record<string, unknown>)?.status as string) || 'success';
        status = statusValue === 'success' ? 'completed' : 'failed';
      }
      // tool_start should NOT reset status (the fix)
    }
    expect(status).toBe('completed');
  });

  it('treats retry_attempt as reasoning progress, not a tool group', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'retry-start',
        content: 'Retry attempt 2/3',
        metadata: { step_type: 'retry_start', ind: 0, turn_sequence: '12', id: 'lg-12' },
      }),
      buildMessage({
        id: 'retry-attempt',
        content: 'Retrying with alternative approach (attempt 2)',
        metadata: { step_type: 'retry_attempt', ind: 0, turn_sequence: '12', id: 'lg-12' },
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups).toHaveLength(1);
    expect(groups[0].primaryType).toBe('reasoning');
    expect(groups[0].messages.map((m) => m.id)).toEqual(['retry-start', 'retry-attempt']);
  });

  it('orders tool/observation groups in the same turn by metadata.sequence before timestamp', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'observation',
        content: 'obs section',
        metadata: {
          step_type: 'observation_delta',
          id: 'obs-1',
          ind: 1,
          turn_sequence: 20,
          sequence: 2002,
          sub_turn_index: 0,
        },
        timestamp: '2026-03-01T10:00:05.000Z',
      }),
      buildMessage({
        id: 'tool-start',
        content: '',
        metadata: {
          step_type: 'tool_start',
          tool_call_id: 'tc-1',
          ind: 1,
          turn_sequence: 20,
          sequence: 2001,
          sub_turn_index: 0,
        },
        timestamp: '2026-03-01T10:00:09.000Z',
      }),
      buildMessage({
        id: 'tool-end',
        content: 'tool output',
        metadata: {
          step_type: 'tool_end',
          tool_call_id: 'tc-1',
          ind: 1,
          turn_sequence: 20,
          sequence: 2001,
          status: 'success',
          sub_turn_index: 0,
        },
        timestamp: '2026-03-01T10:00:09.000Z',
      }),
      buildMessage({
        id: 'observation-end',
        content: '',
        metadata: {
          step_type: 'observation_section_end',
          id: 'obs-1',
          ind: 1,
          turn_sequence: 20,
          sequence: 2002,
          sub_turn_index: 0,
        },
        timestamp: '2026-03-01T10:00:05.000Z',
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups.map((group) => group.primaryType)).toEqual(['tool', 'observation']);
    expect(groups[0].messages[0].id).toBe('tool-start');
    expect(groups[1].messages[0].id).toBe('observation');
  });

  it('keeps alternating tool/observation group order for repeated cycles in one turn', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'obs-2',
        content: 'second observation',
        metadata: {
          step_type: 'observation_delta',
          id: 'obs-2',
          ind: 1,
          turn_sequence: 30,
          sequence: 3004,
          sub_turn_index: 1,
        },
        timestamp: '2026-03-01T10:00:09.000Z',
      }),
      buildMessage({
        id: 'tool-2-start',
        content: '',
        metadata: {
          step_type: 'tool_start',
          tool_call_id: 'tc-2',
          id: 'tool-2',
          ind: 1,
          turn_sequence: 30,
          sequence: 3003,
          sub_turn_index: 1,
        },
        timestamp: '2026-03-01T10:00:10.000Z',
      }),
      buildMessage({
        id: 'obs-1',
        content: 'first observation',
        metadata: {
          step_type: 'observation_delta',
          id: 'obs-1',
          ind: 1,
          turn_sequence: 30,
          sequence: 3002,
          sub_turn_index: 0,
        },
        timestamp: '2026-03-01T10:00:07.000Z',
      }),
      buildMessage({
        id: 'tool-1-start',
        content: '',
        metadata: {
          step_type: 'tool_start',
          tool_call_id: 'tc-1',
          id: 'tool-1',
          ind: 1,
          turn_sequence: 30,
          sequence: 3001,
          sub_turn_index: 0,
        },
        timestamp: '2026-03-01T10:00:08.000Z',
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups.map((group) => group.primaryType)).toEqual([
      'tool',
      'observation',
      'tool',
      'observation',
    ]);
    expect(groups.map((group) => group.messages[0].id)).toEqual([
      'tool-1-start',
      'obs-1',
      'tool-2-start',
      'obs-2',
    ]);
  });

  it('keeps intent-phase reasoning separate from later reasoning in the same turn', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'intent-start',
        metadata: {
          id: 'turn-77',
          step_type: 'reasoning_start',
          ind: 0,
          turn_sequence: 77,
          sub_turn_index: -1,
          reasoning_section_id: 'turn-77:reasoning:0',
          phase_sequence: 0,
        },
      }),
      buildMessage({
        id: 'intent-delta',
        content: 'Analyzing request and deciding execution path.',
        metadata: {
          id: 'turn-77',
          step_type: 'reasoning_delta',
          ind: 0,
          turn_sequence: 77,
          sub_turn_index: -1,
          reasoning_section_id: 'turn-77:reasoning:0',
          phase_sequence: 0,
        },
      }),
      buildMessage({
        id: 'plan-start',
        metadata: {
          id: 'turn-77',
          step_type: 'reasoning_start',
          ind: 0,
          turn_sequence: 77,
          reasoning_section_id: 'turn-77:reasoning:1',
          phase_sequence: 1,
        },
      }),
      buildMessage({
        id: 'plan-delta',
        content: 'Planning the next action.',
        metadata: {
          id: 'turn-77',
          step_type: 'reasoning_delta',
          ind: 0,
          turn_sequence: 77,
          reasoning_section_id: 'turn-77:reasoning:1',
          phase_sequence: 1,
        },
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups).toHaveLength(2);
    expect(groups.every((group) => group.primaryType === 'reasoning')).toBe(true);
    expect(groups[0].messages.map((message) => message.id)).toEqual(['intent-start', 'intent-delta']);
    expect(groups[1].messages.map((message) => message.id)).toEqual(['plan-start', 'plan-delta']);
  });

  it('splits sequential same-turn reasoning sections after reasoning_section_end with shared phase identity', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'section-1-start',
        metadata: {
          id: 'turn-88',
          step_type: 'reasoning_start',
          ind: 0,
          turn_sequence: 88,
          reasoning_section_id: 'turn-88:reasoning:0',
          phase_sequence: 0,
        },
      }),
      buildMessage({
        id: 'section-1-delta',
        content: 'Classifying intent.',
        metadata: {
          id: 'turn-88',
          step_type: 'reasoning_delta',
          ind: 0,
          turn_sequence: 88,
          reasoning_section_id: 'turn-88:reasoning:0',
          phase_sequence: 0,
        },
      }),
      buildMessage({
        id: 'section-1-end',
        metadata: {
          id: 'turn-88',
          step_type: 'reasoning_section_end',
          ind: 0,
          turn_sequence: 88,
          reasoning_section_id: 'turn-88:reasoning:0',
          phase_sequence: 0,
        },
      }),
      buildMessage({
        id: 'section-2-start',
        metadata: {
          id: 'turn-88',
          step_type: 'reasoning_start',
          ind: 0,
          turn_sequence: 88,
          reasoning_section_id: 'turn-88:reasoning:1',
          phase_sequence: 1,
        },
      }),
      buildMessage({
        id: 'section-2-delta',
        content: 'Building execution plan.',
        metadata: {
          id: 'turn-88',
          step_type: 'reasoning_delta',
          ind: 0,
          turn_sequence: 88,
          reasoning_section_id: 'turn-88:reasoning:1',
          phase_sequence: 1,
        },
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups).toHaveLength(2);
    expect(groups.every((group) => group.primaryType === 'reasoning')).toBe(true);
    expect(groups[0].messages.map((message) => message.id)).toEqual([
      'section-1-start',
      'section-1-delta',
      'section-1-end',
    ]);
    expect(groups[1].messages.map((message) => message.id)).toEqual([
      'section-2-start',
      'section-2-delta',
    ]);
  });

  it('keeps legacy reasoning blob groups above canonical tool groups and assistant message groups', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'assistant-final',
        content: 'final answer',
        metadata: {
          step_type: 'assistant_message',
          ind: 2,
          turn_sequence: 91,
          sequence: 9101,
          sequence_authority: 'synthetic_message',
        },
      }),
      buildMessage({
        id: 'tool-start',
        content: '',
        metadata: {
          step_type: 'tool_start',
          tool_call_id: 'tc-91',
          ind: 1,
          turn_sequence: 91,
          sequence: 1,
          sequence_authority: 'canonical_detail',
        },
      }),
      buildMessage({
        id: 'legacy-reasoning-start',
        content: '',
        metadata: {
          step_type: 'reasoning_start',
          ind: 0,
          turn_sequence: 91,
          sequence: 9102,
          sequence_authority: 'legacy_reasoning_blob',
          id: 'legacy-reasoning',
          reasoning_section_id: 'turn-91:reasoning:0',
          phase_sequence: 0,
        },
      }),
      buildMessage({
        id: 'legacy-reasoning-delta',
        content: 'legacy reasoning',
        metadata: {
          step_type: 'reasoning_delta',
          ind: 0,
          turn_sequence: 91,
          sequence: 9102,
          sequence_authority: 'legacy_reasoning_blob',
          id: 'legacy-reasoning',
          reasoning_section_id: 'turn-91:reasoning:0',
          phase_sequence: 0,
        },
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups.map((group) => group.primaryType)).toEqual(['reasoning', 'tool', 'message']);
    expect(groups[0].messages[0].id).toBe('legacy-reasoning-start');
    expect(groups[1].messages[0].id).toBe('tool-start');
    expect(groups[2].messages[0].id).toBe('assistant-final');
  });

  it('groups tool_start, tool_delta, and tool_end into a single batch group by tool_batch_id', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'start-a',
        content: 'Executing nmap...',
        metadata: {
          step_type: 'tool_start',
          tool_batch_id: 'tb-1',
          tool_call_id: 'tc-a',
          ind: 1,
          turn_sequence: '20',
          tool: 'nmap',
        },
      }),
      buildMessage({
        id: 'start-b',
        content: 'Executing httpx...',
        metadata: {
          step_type: 'tool_start',
          tool_batch_id: 'tb-1',
          tool_call_id: 'tc-b',
          ind: 1,
          turn_sequence: '20',
          tool: 'httpx',
        },
      }),
      buildMessage({
        id: 'end-b',
        content: 'Tool httpx completed (success)',
        metadata: {
          step_type: 'tool_end',
          tool_batch_id: 'tb-1',
          tool_call_id: 'tc-b',
          ind: 1,
          turn_sequence: '20',
          tool: 'httpx',
          status: 'success',
        },
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups).toHaveLength(1);
    expect(groups[0].primaryType).toBe('tool');
    expect(groups[0].messages.map((m) => m.id)).toEqual(['start-a', 'start-b', 'end-b']);
  });

  it('renders canonical replayed batch tools as individual tool groups', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'start-a',
        content: 'Executing nmap...',
        metadata: {
          step_type: 'tool_start',
          tool_batch_id: 'tb-history',
          tool_call_id: 'tc-a',
          ind: 1,
          turn_sequence: '22',
          sequence: 100,
          sequence_authority: 'canonical_detail',
          tool: 'nmap',
        },
      }),
      buildMessage({
        id: 'end-a',
        content: 'Tool nmap completed (success)',
        metadata: {
          step_type: 'tool_end',
          tool_batch_id: 'tb-history',
          tool_call_id: 'tc-a',
          ind: 1,
          turn_sequence: '22',
          sequence: 100,
          sequence_authority: 'canonical_detail',
          tool: 'nmap',
          status: 'success',
        },
      }),
      buildMessage({
        id: 'start-b',
        content: 'Executing httpx...',
        metadata: {
          step_type: 'tool_start',
          tool_batch_id: 'tb-history',
          tool_call_id: 'tc-b',
          ind: 1,
          turn_sequence: '22',
          sequence: 101,
          sequence_authority: 'canonical_detail',
          tool: 'httpx',
        },
      }),
      buildMessage({
        id: 'end-b',
        content: 'Tool httpx completed (success)',
        metadata: {
          step_type: 'tool_end',
          tool_batch_id: 'tb-history',
          tool_call_id: 'tc-b',
          ind: 1,
          turn_sequence: '22',
          sequence: 101,
          sequence_authority: 'canonical_detail',
          tool: 'httpx',
          status: 'success',
        },
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups).toHaveLength(2);
    expect(groups.every((group) => group.primaryType === 'tool')).toBe(true);
    expect(groups.map((group) => group.messages.map((message) => message.id))).toEqual([
      ['start-a', 'end-a'],
      ['start-b', 'end-b'],
    ]);
  });

  it('falls back to tool_call_id when tool_batch_id is absent (legacy single-tool runs)', () => {
    const messages: ChatMessage[] = [
      buildMessage({
        id: 'start',
        content: 'Executing nmap...',
        metadata: {
          step_type: 'tool_start',
          tool_call_id: 'call_legacy',
          ind: 1,
          turn_sequence: '21',
          tool: 'nmap',
        },
      }),
      buildMessage({
        id: 'delta',
        content: 'scan output',
        metadata: {
          step_type: 'tool_delta',
          tool_call_id: 'call_legacy',
          ind: 1,
          turn_sequence: '21',
          tool: 'nmap',
        },
      }),
      buildMessage({
        id: 'end',
        content: 'Tool nmap completed (success)',
        metadata: {
          step_type: 'tool_end',
          tool_call_id: 'call_legacy',
          ind: 1,
          turn_sequence: '21',
          tool: 'nmap',
          status: 'success',
        },
      }),
    ];

    const groups = groupMessages(messages);

    expect(groups).toHaveLength(1);
    expect(groups[0].primaryType).toBe('tool');
    expect(groups[0].messages.map((m) => m.id)).toEqual(['start', 'delta', 'end']);
  });
});

describe('STEP_COMPARATOR', () => {
  function buildStep(overrides: Partial<Step> & { __internalKey: string }): Step {
    return {
      type: 'tool_start',
      content: '',
      ...overrides,
    } as Step;
  }

  it('orders tool events by metadata.sequence within same turn (Issue 11 regression)', () => {
    // Replay events share the same turn_sequence, ind, and timestamp but
    // have distinct metadata.sequence values from the backend.
    const toolStart = buildStep({
      __internalKey: 'tool-call_abc-tool_start',
      metadata: { step_type: 'tool_start', turn_sequence: 42, ind: 1, sequence: 43 },
      timestamp: '2024-01-01T00:00:01Z',
    });
    const toolDelta = buildStep({
      __internalKey: 'tool-call_abc-tool_delta',
      metadata: { step_type: 'tool_delta', turn_sequence: 42, ind: 1, sequence: 44 },
      timestamp: '2024-01-01T00:00:01Z',
    });
    const toolEnd = buildStep({
      __internalKey: 'tool-call_abc-tool_end',
      metadata: { step_type: 'tool_end', turn_sequence: 42, ind: 1, sequence: 45 },
      timestamp: '2024-01-01T00:00:01Z',
    });

    // Shuffle into alphabetical key order (the broken sort)
    const steps = [toolDelta, toolEnd, toolStart];
    steps.sort(STEP_COMPARATOR);

    expect(steps.map((s) => s.__internalKey)).toEqual([
      'tool-call_abc-tool_start',
      'tool-call_abc-tool_delta',
      'tool-call_abc-tool_end',
    ]);
  });

  it('uses metadata.sequence before sub_turn_index when both exist (index-base mismatch guard)', () => {
    // Simulate replay data where tool uses 0-based sub_turn_index and observation
    // uses 1-based indices, but metadata.sequence already represents canonical order.
    const toolSecond = buildStep({
      __internalKey: 'tool-call_2-tool_end',
      type: 'tool_end',
      metadata: { step_type: 'tool_end', turn_sequence: 50, ind: 1, sequence: 5003, sub_turn_index: 1 },
      timestamp: '2026-03-01T12:00:03Z',
    });
    const observationFirst = buildStep({
      __internalKey: 'obs-1-observation_delta',
      type: 'observation_delta',
      metadata: { step_type: 'observation_delta', turn_sequence: 50, ind: 1, sequence: 5002, sub_turn_index: 1 },
      timestamp: '2026-03-01T12:00:02Z',
    });
    const toolFirst = buildStep({
      __internalKey: 'tool-call_1-tool_end',
      type: 'tool_end',
      metadata: { step_type: 'tool_end', turn_sequence: 50, ind: 1, sequence: 5001, sub_turn_index: 0 },
      timestamp: '2026-03-01T12:00:01Z',
    });

    const steps = [toolSecond, observationFirst, toolFirst];
    steps.sort(STEP_COMPARATOR);

    expect(steps.map((step) => step.__internalKey)).toEqual([
      'tool-call_1-tool_end',
      'obs-1-observation_delta',
      'tool-call_2-tool_end',
    ]);
  });

  it('does not let legacy blob or synthetic message sequences override semantic phase order', () => {
    const assistantMessage = buildStep({
      __internalKey: 'assistant-message',
      type: 'assistant_message',
      metadata: {
        step_type: 'assistant_message',
        turn_sequence: 91,
        ind: 2,
        sequence: 9101,
        sequence_authority: 'synthetic_message',
      },
      timestamp: '2026-03-01T12:00:03Z',
    });
    const toolStep = buildStep({
      __internalKey: 'tool-step',
      type: 'tool_start',
      metadata: {
        step_type: 'tool_start',
        turn_sequence: 91,
        ind: 1,
        sequence: 1,
        sequence_authority: 'canonical_detail',
      },
      timestamp: '2026-03-01T12:00:02Z',
    });
    const legacyReasoning = buildStep({
      __internalKey: 'legacy-reasoning',
      type: 'reasoning_delta',
      metadata: {
        step_type: 'reasoning_delta',
        turn_sequence: 91,
        ind: 0,
        sequence: 9102,
        sequence_authority: 'legacy_reasoning_blob',
      },
      timestamp: '2026-03-01T12:00:01Z',
    });

    const steps = [assistantMessage, toolStep, legacyReasoning];
    steps.sort(STEP_COMPARATOR);

    expect(steps.map((step) => step.__internalKey)).toEqual([
      'legacy-reasoning',
      'tool-step',
      'assistant-message',
    ]);
  });
});
