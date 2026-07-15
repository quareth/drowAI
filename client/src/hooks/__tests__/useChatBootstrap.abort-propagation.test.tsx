// @vitest-environment jsdom
/**
 * Ensures cancellation propagates through the full bootstrap stack:
 * useChatBootstrap -> apiFetch -> fetch(composed signal).
 */
import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resetInitialHistoryInFlightForTests } from "@/hooks/chat-history-bootstrap";
import { useChatBootstrap } from "@/hooks/useChatBootstrap";

const mocked = vi.hoisted(() => ({
  setConversationId: vi.fn(),
  setChatReadyState: vi.fn(),
  setTaskHistory: vi.fn(),
  setHistoryLoaded: vi.fn(),
  markHistoryBootstrapTerminal: vi.fn(),
  setHistoryLoading: vi.fn(),
  setTranscriptPaginationState: vi.fn(),
  tryStartHistoryLoading: vi.fn(),
  normalizeSSEPayload: vi.fn(
    (payload: { type?: string; content?: string; metadata?: Record<string, unknown> }) => ({
      type: payload.type ?? "status",
      content: payload.content ?? "",
      metadata: payload.metadata ?? {},
      timestamp: undefined,
      isStreaming: false,
    }),
  ),
}));

let streamSnapshot: {
  chatReadyMeta: Record<string, unknown> | null;
  chatReady: boolean;
  isConnected: boolean;
  isConnecting: boolean;
  connectionError: string | null;
  historyLoadedByConversation: Record<string, true>;
  historyBootstrapTerminalByConversation: Record<string, true>;
};

let sessionSnapshot: {
  conversationId: string | null;
};
const historyLoadingByTaskConversation = new Map<string, true>();

function toHistoryKey(taskId: number | null, conversationId?: string | null): string {
  const normalized = typeof conversationId === "string" ? conversationId.trim() : "";
  const conversationKey = normalized.length > 0 ? normalized : "__default__";
  return `${taskId ?? "none"}::${conversationKey}`;
}

vi.mock("@/state/chat-session-store", () => ({
  useChatSessionSnapshot: () => sessionSnapshot,
  setConversationId: mocked.setConversationId,
}));

vi.mock("@/state/chat-stream-store", () => ({
  useTaskStreamSnapshot: () => streamSnapshot,
  setChatReadyState: mocked.setChatReadyState,
  setTaskHistory: mocked.setTaskHistory,
  setTranscriptPaginationState: mocked.setTranscriptPaginationState,
  setHistoryLoaded: (taskId: number, conversationId?: string | null) => {
    mocked.setHistoryLoaded(taskId, conversationId);
    const normalized = typeof conversationId === "string" ? conversationId.trim() : "";
    const key = normalized.length > 0 ? normalized : "__default__";
    streamSnapshot.historyLoadedByConversation[key] = true;
    historyLoadingByTaskConversation.delete(toHistoryKey(taskId, conversationId));
  },
  markHistoryBootstrapTerminal: (taskId: number, conversationId?: string | null) => {
    mocked.markHistoryBootstrapTerminal(taskId, conversationId);
    const normalized = typeof conversationId === "string" ? conversationId.trim() : "";
    const key = normalized.length > 0 ? normalized : "__default__";
    streamSnapshot.historyBootstrapTerminalByConversation[key] = true;
    historyLoadingByTaskConversation.delete(toHistoryKey(taskId, conversationId));
  },
  setHistoryLoading: (taskId: number, isLoading: boolean, conversationId?: string | null) => {
    mocked.setHistoryLoading(taskId, isLoading, conversationId);
    const key = toHistoryKey(taskId, conversationId);
    if (isLoading) {
      historyLoadingByTaskConversation.set(key, true);
      return;
    }
    historyLoadingByTaskConversation.delete(key);
  },
  tryStartHistoryLoading: (taskId: number, conversationId?: string | null) => {
    mocked.tryStartHistoryLoading(taskId, conversationId);
    const key = toHistoryKey(taskId, conversationId);
    if (historyLoadingByTaskConversation.has(key)) {
      return false;
    }
    historyLoadingByTaskConversation.set(key, true);
    return true;
  },
  isConversationHistoryLoading: (taskId: number | null, conversationId?: string | null) =>
    historyLoadingByTaskConversation.has(toHistoryKey(taskId, conversationId)),
}));

function Harness({ taskId, enabled }: { taskId: number | null; enabled: boolean }) {
  useChatBootstrap({ taskId, enabled });
  return null;
}

describe("useChatBootstrap startup request behavior", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    resetInitialHistoryInFlightForTests();
    historyLoadingByTaskConversation.clear();
    streamSnapshot = {
      chatReadyMeta: null,
      chatReady: false,
      isConnected: false,
      isConnecting: false,
      connectionError: null,
      historyLoadedByConversation: {},
      historyBootstrapTerminalByConversation: {},
    };
    sessionSnapshot = { conversationId: null };
  });

  it("starts one startup request per task selection without request storms", async () => {
    streamSnapshot = {
      chatReadyMeta: { task_running: true },
      chatReady: true,
      isConnected: true,
      isConnecting: false,
      connectionError: null,
      historyLoadedByConversation: {},
      historyBootstrapTerminalByConversation: {},
    };
    sessionSnapshot = { conversationId: null };

    const requests: Array<{ url: string; signal: AbortSignal }> = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const signal = init?.signal as AbortSignal;
      requests.push({ url, signal });
      return new Promise<Response>((_resolve, reject) => {
        const rejectWithAbort = () => reject(new DOMException("The operation was aborted", "AbortError"));
        if (signal.aborted) {
          rejectWithAbort();
          return;
        }
        signal.addEventListener("abort", rejectWithAbort, { once: true });
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const view = render(<Harness taskId={987} enabled />);

    await waitFor(() => {
      expect(requests.some((request) => request.url.includes("/api/tasks/987/chat/history?initial=true"))).toBe(true);
    });

    view.rerender(<Harness taskId={988} enabled />);

    await waitFor(() => {
      expect(requests.some((request) => request.url.includes("/api/tasks/988/chat/history?initial=true"))).toBe(true);
    });

    const request987 = requests.find((request) => request.url.includes("/api/tasks/987/chat/history?initial=true"));
    const request988 = requests.find((request) => request.url.includes("/api/tasks/988/chat/history?initial=true"));
    expect(request987).toBeDefined();
    expect(request988).toBeDefined();
    expect(requests).toHaveLength(2);
  });

  it("dedupes concurrent startup mounts for the same task and conversation", async () => {
    streamSnapshot = {
      chatReadyMeta: null,
      chatReady: false,
      isConnected: false,
      isConnecting: false,
      connectionError: null,
      historyLoadedByConversation: {},
      historyBootstrapTerminalByConversation: {},
    };
    sessionSnapshot = { conversationId: "conv-shared" };

    const startupResponse = new Response(
      JSON.stringify({
        startup: {
          task_id: 771,
          conversation_id: "conv-shared",
          checkpointer_ready: true,
          tool_catalog_ready: true,
          pty_session_ready: false,
          runtime_warm: true,
          pty_warmup_required: false,
          task_running: true,
          sse_connected: true,
          chat_ready: true,
        },
        items: [],
        nextBeforeTurn: null,
        hasMoreOlder: false,
      }),
      { status: 200 },
    );
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(startupResponse.clone()));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <>
        <Harness taskId={771} enabled />
        <Harness taskId={771} enabled />
      </>,
    );

    await waitFor(() => {
      expect(mocked.setHistoryLoaded).toHaveBeenCalledWith(771, "conv-shared");
    });

    const startupCalls = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes("/api/tasks/771/chat/history?initial=true&conversation_id=conv-shared"),
    );
    expect(startupCalls).toHaveLength(1);
  });
});
