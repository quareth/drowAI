/**
 * Runtime stream protocol contract for multiplex WebSocket reasoning streams.
 *
 * Responsibility:
 * - Define request/response/event envelopes shared by runtime stream components.
 * - Keep field names aligned with backend `/ws` agent-multi protocol.
 */

export type RuntimeStreamChannel = "agent";

export interface RuntimeStreamSubscribeRequest {
  action: "subscribe";
  channel: RuntimeStreamChannel;
  taskId: number;
  last_seen_sequence?: number;
}

export interface RuntimeStreamUnsubscribeRequest {
  action: "unsubscribe";
  channel: RuntimeStreamChannel;
  taskId: number;
}

export interface RuntimeStreamPing {
  type: "ping";
}

export interface RuntimeStreamConnectionEstablished {
  type: "connection_established";
  connection: string;
}

export interface RuntimeStreamPong {
  type: "pong";
}

export interface RuntimeStreamSubscribed {
  type: "subscribed";
  taskId: number;
}

export interface RuntimeStreamUnsubscribed {
  type: "unsubscribed";
  taskId: number;
}

export interface RuntimeStreamError {
  type: "error";
  message: string;
  code?: string;
  taskId?: number;
}

export type RuntimeStreamAuthFailureReason =
  | "missing_token"
  | "token_expired"
  | "invalid_token"
  | "missing_exp"
  | "unauthorized_identity"
  | "unknown_auth_error";

export type RuntimeTaskSubscriptionPhase =
  | "idle"
  | "pending_subscribe"
  | "subscribed"
  | "pending_unsubscribe"
  | "error";

export type RuntimeTaskSubscriptionErrorReason =
  | "forbidden_task"
  | "max_subscriptions"
  | "invalid_task_id"
  | "subscribe_failed"
  | "subscribe_timeout"
  | "unknown_error";

export interface RuntimeTaskSubscriptionState {
  taskId: number;
  desired: boolean;
  phase: RuntimeTaskSubscriptionPhase;
  errorReason: RuntimeTaskSubscriptionErrorReason | null;
  updatedAt: number;
}

export interface RuntimeAgentReasoningEnvelope<TPacket extends Record<string, unknown> = Record<string, unknown>> {
  type: "agent_reasoning";
  taskId: number;
  sequence: number;
  packet: TPacket;
}

export type RuntimeStreamClientMessage =
  | RuntimeStreamSubscribeRequest
  | RuntimeStreamUnsubscribeRequest
  | RuntimeStreamPing;

export type RuntimeStreamServerControlMessage =
  | RuntimeStreamConnectionEstablished
  | RuntimeStreamPong
  | RuntimeStreamSubscribed
  | RuntimeStreamUnsubscribed
  | RuntimeStreamError;

export type RuntimeStreamServerMessage<TPacket extends Record<string, unknown> = Record<string, unknown>> =
  | RuntimeStreamServerControlMessage
  | RuntimeAgentReasoningEnvelope<TPacket>;
