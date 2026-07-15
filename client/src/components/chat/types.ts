/**
 * Shared chat type definitions for the unified agent chat interface.
 *
 * SSE→ChatMessage Normalization Contract:
 * - Source SSE events follow the shape { type, content, metadata, timestamp }
 *   with monotonically increasing numeric metadata.sequence values and
 *   optional OpenAI-style delta chunks for streaming completions.
 * - Each SSE payload must map to a ChatMessage with the same id used for
 *   future partial updates. Streaming updates set isStreaming=true until a
 *   terminating chunk (the "[DONE]" sentinel) is received.
 * - metadata.status reflects execution state emitted by the server (pending →
 *   success | error). Tools or command outputs populate metadata.command and
 *   metadata.canExpand to control expandable message UI.
 * - Consumers may emit synthetic "user" messages, but must ensure they still
 *   traverse the /chat pipeline so that the persisted transcript stays in
 *   ChatMessage.
 */

import type {
  CompactToolResult,
} from "@/types/compact-tool-result";

export type {
  CompactToolArtifactReference,
  CompactToolCompressionMetadata,
  CompactToolResult,
} from "@/types/compact-tool-result";

export type ChatMessageType =
  | 'user'
  | 'agent'
  | 'system'
  | 'thinking'
  | 'executing';

export type ChatMessageStatus = 'pending' | 'success' | 'error' | 'declined';

export interface ProviderRefusalMetadata {
  provider: string;
  model: string;
  category?: string | null;
  summary: string;
  explanation?: string | null;
  response_id?: string | null;
  partial: boolean;
}

/**
 * Canonical lifecycle steps for tool execution events used by tool cards.
 * Task 1.1 contract: raw-output lookup relies on metadata.tool_call_id emitted
 * on both tool_start and tool_end events.
 */
export type ToolLifecycleStepType = "tool_start" | "tool_end";

/**
 * Backend tool status values are additive over time; preserve unknown strings.
 * Task 1.1 contract: tool_end status must be stable/passthrough for deterministic
 * client mapping and persisted replay behavior.
 */
export type ToolLifecycleStatus = "success" | "error" | "failed" | "ok" | "unknown" | string;

export interface ChatMessageMetadata {
  /** Optional step classification (e.g. reasoning, action, reflection). */
  stepType?: string;
  /** Raw backend step_type for stream-event alignment. */
  step_type?: string;
  /** Phase index for grouping related events (reasoning/tool/answer). */
  ind?: number;
  /** Internal-only events should not render in the chat transcript. */
  internal_only?: boolean;
  /** Allow UI to toggle expandable command/output sections. */
  canExpand?: boolean;
  /** Command or tool identifier associated with this message. */
  command?: string;
  /** Execution status emitted by the backend for actionable steps. */
  status?: ChatMessageStatus | ToolLifecycleStatus;
  /** Canonical per-turn sequence used for ordering grouped cards. */
  turn_sequence?: number;
  /** Legacy DB-backed sequence used for resume/dedupe. */
  sequence?: number;
  /** Declares whether ``metadata.sequence`` is canonical or synthetic. */
  sequence_authority?: string;
  /** Canonical display order of a card within a turn. */
  phase_sequence?: number;
  /** Stable identity shared by start/delta/end events for one thinking card. */
  reasoning_section_id?: string;
  /**
   * Stable tool call identifier emitted on tool lifecycle events.
   * Contract for raw lookup: must remain identical across tool_start/tool_end.
   */
  tool_call_id?: string;
  /**
   * Task scope used by provenance fetchers. Route task context is canonical;
   * metadata.task_id may be forwarded by backend stream events when available.
   */
  task_id?: string | number;
  /** Retry-specific metadata fields. */
  retryable?: boolean;
  retry_mode?: string;
  error_code?: string;
  error_message?: string;
  graph_name?: string;
  turn_id?: string;
  retry_attempt?: number;
  retry_max_attempts?: number;
  retry_failure_category?: string;
  retry_alternative_tool?: string;
  /** Provider-neutral terminal refusal projection. */
  outcome_type?: string;
  stop_reason?: string;
  refusal?: ProviderRefusalMetadata;
  /** Client-side message id for optimistic reconciliation. */
  client_message_id?: string;
  /** DR iteration index for separating observation cards per iteration. */
  sub_turn_index?: number;
  /** Legacy/extended fields emitted by backend stream metadata. */
  id?: string;
  tool_name?: string;
  tool?: string;
  summary?:
    | {
        stdout_excerpt?: string;
        stderr_excerpt?: string;
        observation?: string;
      }
    | CompactToolResult;
  compact_tool_result?: CompactToolResult | null;
  [key: string]: unknown;
}

export interface ChatMessage {
  /** Stable identifier used for reconciliation and streaming updates. */
  id: string;
  /** Message author or role used to drive presentation logic. */
  type: ChatMessageType;
  /** Fully rendered message content (markdown already normalized). */
  content: string;
  /** RFC 3339 timestamp denoting when the message was emitted. */
  timestamp: string;
  /** Indicates that additional streaming chunks are expected. */
  isStreaming?: boolean;
  /** Optional structured metadata controlling presentation details. */
  metadata?: ChatMessageMetadata;
}

/**
 * Legacy primary-mode contract.
 *
 * Phase 6 splits the primary dropdown into (primary, plan) composite
 * state: a ``ChatPrimaryMode`` value plus a boolean ``planMode``
 * toggle. ``ChatExperienceMode`` is retained for hydration and
 * persistence of legacy client state — the ``plan`` value is
 * deprecated for emission and only accepted on load, where it
 * hydrates into ``(agent, planMode=true)``. The new UI never emits
 * ``plan`` as a primary mode.
 */
export type ChatExperienceMode =
  | 'plan'
  | 'chat'
  | 'agent'
  | 'agent_full'
  | 'agent_full_plan';

/**
 * Primary chat mode as exposed in the new UI dropdown.
 *
 * Plan is a route overlay (``ChatPlanMode``), not a primary mode:
 * it stacks on top of ``agent`` or ``agent_full`` via a separate
 * adjacent toggle. ``chat`` is mutually exclusive with plan.
 */
export type ChatPrimaryMode = 'chat' | 'agent' | 'agent_full';

/** Boolean route-overlay state for Plan. */
export type ChatPlanMode = boolean;

/**
 * Composite chat-mode selection used by the new UI.
 *
 * ``plan`` is a route overlay stacked on top of ``primary``. The UI
 * keeps the two as independent fields and derives an
 * ``agent_mode`` + ``plan_mode`` payload at send time. Legacy
 * ``ChatExperienceMode`` can be converted via
 * {@link chatExperienceModeToComposite}.
 */
export interface ChatModeSelection {
  primary: ChatPrimaryMode;
  plan: ChatPlanMode;
}

/**
 * Hydrate legacy ``ChatExperienceMode`` into the composite shape.
 *
 * Legacy ``plan`` hydrates as ``(agent, plan=true)`` so migration
 * does not lose the user's selection. Unknown values fall back to
 * ``(agent, plan=false)`` to preserve reasonable defaults.
 */
export function chatExperienceModeToComposite(
  mode: ChatExperienceMode,
): ChatModeSelection {
  switch (mode) {
    case 'chat':
      return { primary: 'chat', plan: false };
    case 'agent':
      return { primary: 'agent', plan: false };
    case 'agent_full':
      return { primary: 'agent_full', plan: false };
    case 'agent_full_plan':
      return { primary: 'agent_full', plan: true };
    case 'plan':
      // Legacy selection: hydrate as ``agent + plan`` so the user
      // keeps a meaningful overlay after the UX migration.
      return { primary: 'agent', plan: true };
    default:
      return { primary: 'agent', plan: false };
  }
}

/**
 * Convert a composite selection back into a legacy ``ChatExperienceMode``.
 *
 * This lets the new UI persist its state through existing persistence
 * surfaces (e.g. page-level state) without introducing a second
 * durable representation. The round-trip is lossless for the
 * four-tier contract.
 */
export function compositeToChatExperienceMode(
  selection: ChatModeSelection,
): ChatExperienceMode {
  if (selection.primary === 'chat') {
    return 'chat';
  }
  if (selection.plan) {
    return selection.primary === 'agent_full' ? 'agent_full_plan' : 'plan';
  }
  return selection.primary;
}

/**
 * Build the transport-shaped ``agent_mode`` + ``plan_mode`` payload
 * from a composite selection.
 *
 * Maps primary-mode keys into the stable ``agent_mode`` wire values
 * and stacks ``plan_mode`` as a separate boolean. The new UI never
 * emits ``agent_mode=plan``.
 */
export function chatSelectionToAgentModePayload(
  selection: ChatModeSelection,
): { agent_mode: 'chat' | 'agent' | 'full_access'; plan_mode: boolean } {
  if (selection.primary === 'chat') {
    // ``chat`` is mutually exclusive with plan at the UI layer; the
    // backend enforces the same invariant. We still clear plan here
    // so a miswired caller cannot smuggle ``chat + plan`` through.
    return { agent_mode: 'chat', plan_mode: false };
  }
  return {
    agent_mode: selection.primary === 'agent_full' ? 'full_access' : 'agent',
    plan_mode: Boolean(selection.plan),
  };
}

export interface ChatMode {
  /** Whether the current user can submit new messages. */
  canSendMessages: boolean;
  /** Placeholder text rendered in the chat input control. */
  inputPlaceholder: string;
  /** Disable state for the chat input when sending is disallowed. */
  inputDisabled: boolean;
}

export interface MessageProvider {
  /** Ordered list of chat messages scoped to the current task. */
  messages: ChatMessage[];
  /** Indicates whether historical messages are currently loading. */
  isLoading: boolean;
  /** Live connection state for SSE or WebSocket streams. */
  isConnected: boolean;
  /** Optional last error encountered while streaming messages. */
  connectionError?: string | null;
  /** Send a new user-authored message to the backend. */
  sendMessage: (content: string) => Promise<void>;
  /** Fetch additional historical messages (older than current list). */
  loadMore: () => Promise<void>;
  /** Flag indicating the availability of more historical messages. */
  hasMore: boolean;
}

export interface MessageProviderFactory {
  /**
   * Build a MessageProvider for a given task identifier and execution mode.
   * Enables dependency injection for testing and alternate data sources.
   */
  create: (taskId: string) => MessageProvider;
}

export type SendMessageFn = MessageProvider['sendMessage'];
export type LoadMoreFn = MessageProvider['loadMore'];
