/**
 * Stream event helpers built on the generated packet types.
 *
 * Base event typings are generated from the backend Pydantic schema in
 * `client/src/types/packets.ts`.
 */

import type {
  StreamEvent,
  StreamEventMetadata,
  StreamEventType,
} from "./packets";

export type { StreamEvent, StreamEventMetadata, StreamEventType };

export type PersistedStreamEvent = StreamEvent & {
  metadata: StreamEventMetadata & { streaming?: false };
};

export type StreamingDeltaEvent = StreamEvent & {
  metadata: StreamEventMetadata & { streaming?: true };
};

export function isSnapshot(event: StreamEvent): event is PersistedStreamEvent {
  return Boolean(event.metadata && event.metadata.streaming === false);
}

export function isStreamingDelta(event: StreamEvent): event is StreamingDeltaEvent {
  return Boolean(event.metadata && event.metadata.streaming !== false);
}
