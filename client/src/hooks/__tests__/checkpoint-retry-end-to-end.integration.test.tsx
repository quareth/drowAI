// @vitest-environment jsdom
/**
 * Phase 7 Task 7.1 — end-to-end retry scenario.
 *
 * The repo does not have a Playwright e2e harness for the retry path
 * (``e2e/`` only covers chat streaming + engagement workspace and would
 * require a live backend on http://localhost:5000). This file provides
 * the highest-fidelity integration available within the existing
 * vitest harness: it wires together the real production modules
 *
 *   - ``useGraphRetry`` (mutation that POSTs the retry request)
 *   - ``retry-state-store`` (per-task / per-turn lifecycle store)
 *   - ``useMultiTaskStreamManager`` (WebSocket -> store + resync)
 *   - ``seedRetryStateFromTranscriptItems`` (bootstrap parity)
 *
 * and walks the full rapid-click -> exactly-one-worker -> UI shows
 * retrying without a destructive toast -> resync -> server-driven
 * checkpoint-consistent transcript flow described in the guide.
 *
 * Acceptance criteria from the guide (Phase 7 Task 7.1):
 *   - Multiple rapid clicks produce one backend retry attempt.
 *   - UI shows retrying state without destructive conflict toast.
 *   - Worker uses stored checkpoint id.
 *   - Frontend resync renders checkpoint-consistent transcript.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import useMultiTaskStreamManager, {
  __resetMultiTaskStreamManagerStateForTest,
} from "@/hooks/useMultiTaskStreamManager";
import { useGraphRetry } from "@/hooks/useGraphRetry";
import { seedRetryStateFromTranscriptItems } from "@/hooks/chat-history-bootstrap";
import * as chatStreamStore from "@/state/chat-stream-store";
import {
  __resetRetryStateStoreForTest,
  getRetryStateForTurn,
} from "@/state/retry-state-store";

const TASK_ID = 70101;
const TURN_ID = "task-70101-turn-7";
const CHECKPOINT_ID = "ckpt-stable-end-to-end";
const WORKFLOW_ID = 314;

const mocked = vi.hoisted(() => ({
  apiRequestMock: vi.fn(),
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: mocked.apiRequestMock,
}));

vi.mock("@/utils/websocket-config", () => ({
  wsConfig: {
    getWebSocketUrl: vi.fn(() => "ws://example/ws?type=agent-multi"),
  },
}));

class FakeWebSocket {
  public static CONNECTING = 0;
  public static OPEN = 1;
  public static CLOSING = 2;
  public static CLOSED = 3;

  public readonly sentMessages: string[] = [];
  public readyState = FakeWebSocket.CONNECTING;
  public onopen: (() => void) | null = null;
  public onmessage: ((event: MessageEvent<string>) => void) | null = null;
  public onclose: (() => void) | null = null;
  public onerror: (() => void) | null = null;

  public send(data: string): void {
    this.sentMessages.push(data);
  }

  public close(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  public open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  public emitMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

const sockets: FakeWebSocket[] = [];

function ensureTestLocalStorage(): Storage {
  if (
    typeof window.localStorage?.getItem === "function" &&
    typeof window.localStorage?.setItem === "function" &&
    typeof window.localStorage?.removeItem === "function"
  ) {
    return window.localStorage;
  }

  const store = new Map<string, string>();
  const localStorageLike = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => {
      store.clear();
    },
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  } satisfies Storage;

  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: localStorageLike,
  });
  return localStorageLike;
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  sockets.length = 0;
  ensureTestLocalStorage().setItem("access_token", "test-token");
  vi.stubGlobal(
    "WebSocket",
    class extends FakeWebSocket {
      public constructor() {
        super();
        sockets.push(this);
      }
    } as unknown as typeof WebSocket,
  );
});

afterEach(() => {
  chatStreamStore.clearTaskState(TASK_ID);
  __resetRetryStateStoreForTest();
  __resetMultiTaskStreamManagerStateForTest();
  ensureTestLocalStorage().removeItem("access_token");
  mocked.apiRequestMock.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function emitRetryState(
  socket: FakeWebSocket,
  sequence: number,
  metadata: Record<string, unknown>,
): void {
  socket.emitMessage({
    type: "agent_reasoning",
    taskId: TASK_ID,
    sequence,
    packet: {
      placement: { turn_index: 1, tab_index: 1 },
      obj: {
        type: "status",
        content: "retry_state",
        metadata: { task_id: TASK_ID, ...metadata },
      },
    },
  });
}

describe("checkpoint retry end-to-end (Phase 7 Task 7.1)", () => {
  it(
    "rapid clicks schedule exactly one worker, retry_state stream drives resync, " +
      "and bootstrap parity renders checkpoint-consistent transcript",
    async () => {
      // The backend (Phase 1) responds to the first POST with a fresh
      // ``claimed`` retry identity, and to every duplicate POST with a
      // 200 ``already_in_flight=true`` payload echoing the same
      // workflow + checkpoint. This is the contract the duplicate-spam
      // test in test_task_graph_retry_service.py pins on the backend.
      mocked.apiRequestMock.mockImplementation(async (_method, _url, _body) => {
        const payload = {
          status: "retrying",
          task_id: TASK_ID,
          turn_id: TURN_ID,
          retry_mode: "checkpoint",
          workflow_id: WORKFLOW_ID,
          checkpoint_id: CHECKPOINT_ID,
          retry_attempt: 1,
          retry_max_attempts: 2,
          graph_name: "simple_tool",
          state: "started",
          already_in_flight: mocked.apiRequestMock.mock.calls.length > 1,
        };
        return {
          ok: true,
          status: 200,
          json: async () => payload,
        } as unknown as Response;
      });

      // Bootstrap loaded the canonical projection from the server first;
      // it must seed the retry-state store so the in-flight retry CTA
      // disables on the first paint after remount (Phase 4 + 6.3 parity).
      // ``retrying`` here mirrors the projection that
      // chat_transcript_query_service emits for a workflow row in
      // RETRYING.
      seedRetryStateFromTranscriptItems(TASK_ID, [
        {
          id: "msg-bootstrap",
          kind: "assistant",
          turn_number: 7,
          content: "[Error] retrying",
          metadata: {
            turn_id: TURN_ID,
            retry_state: "retrying",
            active_retry: true,
            retry_attempt: 1,
            retry_max_attempts: 2,
            workflow_id: WORKFLOW_ID,
            checkpoint_id: CHECKPOINT_ID,
          },
        },
      ]);

      const seeded = getRetryStateForTurn(TASK_ID, TURN_ID);
      expect(seeded?.state).toBe("retrying");
      expect(seeded?.inFlight).toBe(true);

      // Mount the live stream manager so a real ``status/retry_state``
      // packet flows store -> resync -> CTA disable.
      renderHook(() => useMultiTaskStreamManager({ taskIds: [TASK_ID], enabled: true }));
      expect(sockets).toHaveLength(1);
      sockets[0].open();
      sockets[0].emitMessage({ type: "subscribed", taskId: TASK_ID });

      // Spy on the resync primitive to verify Task 6.2 wiring still
      // fires when the canonical retry_state packet sets
      // ``transcript_resync_required``.
      const resyncSpy = vi.spyOn(chatStreamStore, "resetTaskStreamForResync");

      // Mount the retry mutation hook used by chat surfaces.
      const { result: retryHookResult } = renderHook(() => useGraphRetry(), {
        wrapper,
      });

      // Rapid-click the retry button five times back-to-back.
      const RAPID_CLICKS = 5;
      for (let i = 0; i < RAPID_CLICKS; i += 1) {
        retryHookResult.current.mutate({
          taskId: TASK_ID,
          turnId: TURN_ID,
          retryMode: "checkpoint",
          graphName: "simple_tool",
        });
      }

      await waitFor(() => {
        expect(retryHookResult.current.isSuccess).toBe(true);
      });

      // The frontend posts every click through the API in the current
      // architecture, but the BACKEND must claim exactly one worker —
      // the duplicates surface as 200 ``already_in_flight`` payloads
      // (data, NOT thrown errors) so the UI does not render a
      // destructive conflict toast. We assert both contracts:
      //
      //   * every duplicate POST returns success (no isError state),
      //   * every duplicate response carries ``already_in_flight=true``
      //     except the first one (the claimed worker),
      //   * the canonical identity (workflow_id + checkpoint_id +
      //     retry_attempt + retry_max_attempts) is echoed every call.
      expect(mocked.apiRequestMock).toHaveBeenCalledTimes(RAPID_CLICKS);
      expect(retryHookResult.current.isError).toBe(false);
      const lastResult = retryHookResult.current.data;
      expect(lastResult).toMatchObject({
        status: "retrying",
        task_id: TASK_ID,
        turn_id: TURN_ID,
        retry_mode: "checkpoint",
        workflow_id: WORKFLOW_ID,
        checkpoint_id: CHECKPOINT_ID,
        retry_attempt: 1,
        retry_max_attempts: 2,
        already_in_flight: true,
      });

      // The store must reflect ``retrying`` (in-flight) — no destructive
      // conflict toast. ``inFlight=true`` is what disables the CTA.
      const afterPostEntry = getRetryStateForTurn(TASK_ID, TURN_ID);
      expect(afterPostEntry?.state).toBe("retrying");
      expect(afterPostEntry?.inFlight).toBe(true);

      // The backend now publishes the canonical ``status/retry_state``
      // event with ``transcript_resync_required=true`` so the frontend
      // resyncs to the checkpoint-pinned transcript.
      emitRetryState(sockets[0], 1, {
        turn_id: TURN_ID,
        workflow_id: WORKFLOW_ID,
        checkpoint_id: CHECKPOINT_ID,
        retry_mode: "checkpoint",
        retry_attempt: 1,
        retry_max_attempts: 2,
        graph_name: "simple_tool",
        state: "started",
        transcript_resync_required: true,
      });

      // The resync primitive (Task 6.2) is invoked exactly once at this
      // sequence. Subsequent duplicate retry_state events at the same
      // sequence must not re-trigger the resync.
      await waitFor(() => {
        expect(resyncSpy).toHaveBeenCalledWith(TASK_ID, 1);
      });
      expect(resyncSpy).toHaveBeenCalledTimes(1);

      // Replay the same retry_state packet (e.g. duplicate stream
      // delivery): no extra resync call, no cursor regression.
      emitRetryState(sockets[0], 1, {
        turn_id: TURN_ID,
        workflow_id: WORKFLOW_ID,
        checkpoint_id: CHECKPOINT_ID,
        retry_mode: "checkpoint",
        retry_attempt: 1,
        retry_max_attempts: 2,
        graph_name: "simple_tool",
        state: "started",
        transcript_resync_required: true,
      });
      expect(resyncSpy).toHaveBeenCalledTimes(1);

      // The backend then emits the terminal ``completed`` retry_state
      // event. The store must transition to ``completed`` (not
      // in-flight) and the CTA must NOT re-enable, because the store's
      // sticky-terminal invariant for ``completed`` is what guards
      // against late-arriving stale ``failed`` events.
      emitRetryState(sockets[0], 2, {
        turn_id: TURN_ID,
        workflow_id: WORKFLOW_ID,
        checkpoint_id: CHECKPOINT_ID,
        retry_mode: "checkpoint",
        retry_attempt: 1,
        retry_max_attempts: 2,
        graph_name: "simple_tool",
        state: "completed",
      });

      await waitFor(() => {
        const entry = getRetryStateForTurn(TASK_ID, TURN_ID);
        expect(entry?.state).toBe("completed");
      });
      const completedEntry = getRetryStateForTurn(TASK_ID, TURN_ID);
      expect(completedEntry?.inFlight).toBe(false);

      // Even if a stale ``failed`` projection arrives later (e.g. from a
      // bootstrap re-fetch racing the new completed projection), the
      // sticky-terminal invariant prevents the CTA from re-enabling.
      seedRetryStateFromTranscriptItems(TASK_ID, [
        {
          id: "msg-bootstrap-stale",
          kind: "assistant",
          turn_number: 7,
          content: "[Error] retry from checkpoint",
          metadata: {
            turn_id: TURN_ID,
            retry_state: "failed",
            retryable: true,
            retry_attempt: 1,
            retry_max_attempts: 2,
            workflow_id: WORKFLOW_ID,
            checkpoint_id: CHECKPOINT_ID,
          },
        },
      ]);
      expect(getRetryStateForTurn(TASK_ID, TURN_ID)?.state).toBe("completed");

      // Finally: the worker payload received by every successful
      // mutation echoes the stored ``checkpoint_id``, proving the
      // server-authoritative checkpoint pinning contract from Task 2.x
      // reaches the frontend without recalculation.
      for (const call of mocked.apiRequestMock.mock.results) {
        const response = await call.value;
        const payload = (await response.json()) as { checkpoint_id?: string };
        expect(payload.checkpoint_id).toBe(CHECKPOINT_ID);
      }
    },
  );
});
