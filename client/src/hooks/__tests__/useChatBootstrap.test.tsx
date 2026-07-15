// @vitest-environment jsdom
import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resetInitialHistoryInFlightForTests } from "@/hooks/chat-history-bootstrap";
import { type ChatBootstrapState, useChatBootstrap } from "@/hooks/useChatBootstrap";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
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

streamSnapshot = {
  chatReadyMeta: null,
  chatReady: false,
  isConnected: false,
  isConnecting: false,
  connectionError: null,
  historyLoadedByConversation: {},
  historyBootstrapTerminalByConversation: {},
};
sessionSnapshot = {
  conversationId: null,
};

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

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

function StateHarness(
  { taskId, enabled, onState }: { taskId: number | null; enabled: boolean; onState: (state: ChatBootstrapState) => void },
) {
  const state = useChatBootstrap({ taskId, enabled });
  onState(state);
  return null;
}

describe("useChatBootstrap single-startup flow", () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
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
    sessionSnapshot = {
      conversationId: null,
    };
  });

  it("resolves bootstrap payload, hydrates history, and stores conversation id", async () => {
    mocked.apiFetch.mockResolvedValue(
      new Response(
        JSON.stringify({
          startup: {
            task_id: 501,
            conversation_id: "conv-501",
            checkpointer_ready: true,
            tool_catalog_ready: true,
            pty_session_ready: false,
            runtime_warm: true,
            pty_warmup_required: false,
            task_running: true,
            sse_connected: true,
            chat_ready: true,
          },
          items: [
            {
              id: "item-1",
              kind: "tool",
              turn_number: 1,
              content: "nmap output",
              metadata: {
                sequence: 1,
                tool_call_id: "tc-501",
              },
            },
          ],
          nextBeforeTurn: null,
          hasMoreOlder: false,
        }),
        { status: 200 },
      ),
    );

    render(<Harness taskId={501} enabled />);

    await waitFor(() => {
      expect(mocked.apiFetch).toHaveBeenCalledTimes(1);
    });
    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/tasks/501/chat/history?initial=true",
      expect.objectContaining({ method: "GET" }),
    );
    const startupRequestUrl = String(mocked.apiFetch.mock.calls[0]?.[0] ?? "");
    expect(startupRequestUrl).toContain("initial=true");
    expect(startupRequestUrl).not.toContain("after=");
    expect(startupRequestUrl).not.toContain("before_turn=");
    expect(mocked.setChatReadyState).toHaveBeenCalledWith(
      501,
      true,
      expect.objectContaining({ conversation_id: "conv-501" }),
    );
    expect(mocked.setConversationId).toHaveBeenCalledWith(501, "conv-501");
    expect(mocked.setTaskHistory).toHaveBeenCalledTimes(1);
    expect(mocked.setTaskHistory).toHaveBeenCalledWith(
      501,
      expect.any(Array),
      expect.objectContaining({
        markHistoryLoaded: false,
        conversationId: "conv-501",
      }),
    );
    expect(mocked.setHistoryLoaded).toHaveBeenCalledWith(501, "conv-501");
    expect(mocked.setTranscriptPaginationState).toHaveBeenCalledWith(
      501,
      expect.objectContaining({
        conversationId: "conv-501",
        hasMoreOlder: false,
        nextBeforeCursor: null,
      }),
    );
  });

  it("does not chain continuation requests from bootstrap", async () => {
    mocked.apiFetch.mockResolvedValue(
      new Response(
        JSON.stringify({
          startup: {
            task_id: 920,
            conversation_id: "conv-920",
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
      ),
    );

    render(<Harness taskId={920} enabled />);

    await waitFor(() => {
      expect(mocked.setHistoryLoaded).toHaveBeenCalledWith(920, "conv-920");
    });

    const historyCalls = mocked.apiFetch.mock.calls.filter(
      ([url]) => typeof url === "string" && url.includes("/chat/history"),
    );
    expect(historyCalls).toHaveLength(1);
    expect(historyCalls.some(([url]) => String(url).includes("initial=true"))).toBe(true);
    expect(historyCalls.some(([url]) => String(url).includes("after=200"))).toBe(false);
    for (const [url] of historyCalls) {
      expect(String(url)).not.toContain("source=");
    }
  });

  it("skips startup request when conversation history is already loaded", async () => {
    sessionSnapshot = { conversationId: "cached-conversation" };
    streamSnapshot.historyLoadedByConversation = { "cached-conversation": true };

    render(<Harness taskId={777} enabled />);

    await waitFor(() => {
      expect(mocked.apiFetch).not.toHaveBeenCalled();
    });
  });

  it("skips startup request while the same conversation is already loading", async () => {
    sessionSnapshot = { conversationId: "conv-loading" };
    historyLoadingByTaskConversation.set(toHistoryKey(778, "conv-loading"), true);

    render(<Harness taskId={778} enabled />);

    await waitFor(() => {
      expect(mocked.apiFetch).not.toHaveBeenCalled();
    });
  });

  it("starts a fresh startup request when task changes", async () => {
    const capturedSignals: AbortSignal[] = [];
    mocked.apiFetch.mockImplementation((_url: string, init?: RequestInit) => {
      const signal = init?.signal as AbortSignal;
      capturedSignals.push(signal);
      return new Promise<Response>((_resolve, reject) => {
        if (signal.aborted) {
          reject(new DOMException("aborted", "AbortError"));
          return;
        }
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });

    const view = render(<Harness taskId={602} enabled />);
    await waitFor(() => {
      expect(capturedSignals.length).toBe(1);
    });

    view.rerender(<Harness taskId={603} enabled />);
    await waitFor(() => {
      expect(capturedSignals.length).toBe(2);
    });

    expect(capturedSignals[0].aborted).toBe(false);
  });

  it("emits error state when bootstrap request fails", async () => {
    const states: ChatBootstrapState[] = [];
    mocked.apiFetch.mockResolvedValue(new Response("boom", { status: 500 }));

    render(
      <StateHarness
        taskId={904}
        enabled
        onState={(state) => {
          states.push(state);
        }}
      />,
    );

    await waitFor(() => {
      expect(states.some((state) => Boolean(state.error))).toBe(true);
    });
    expect(states[states.length - 1].isReady).toBe(false);
    expect(states[states.length - 1].isPending).toBe(false);
  });

  it("marks bootstrap terminal on 404 and avoids refetch storms", async () => {
    mocked.apiFetch.mockResolvedValue(new Response("missing", { status: 404 }));

    render(<Harness taskId={905} enabled />);

    await waitFor(() => {
      expect(mocked.markHistoryBootstrapTerminal).toHaveBeenCalledWith(905, null);
    });

    await waitFor(() => {
      expect(mocked.apiFetch).toHaveBeenCalledTimes(1);
    });
  });

  it("maps max_subscriptions connection error to actionable selected-task guidance", async () => {
    const states: ChatBootstrapState[] = [];
    streamSnapshot = {
      chatReadyMeta: { conversation_id: "conv-910", task_running: true },
      chatReady: false,
      isConnected: false,
      isConnecting: false,
      connectionError: "max_subscriptions",
      historyLoadedByConversation: { "conv-910": true },
      historyBootstrapTerminalByConversation: {},
    };
    sessionSnapshot = {
      conversationId: "conv-910",
    };

    render(
      <StateHarness
        taskId={910}
        enabled
        onState={(state) => {
          states.push(state);
        }}
      />,
    );

    await waitFor(() => {
      expect(states.length).toBeGreaterThan(0);
    });

    const latest = states[states.length - 1];
    expect(latest.error).toBe(
      "Live stream limit reached for this task. Pause or stop another active task, then try again.",
    );
    expect(latest.statusMessage).toBe(
      "Live stream limit reached for this task. Pause or stop another active task, then try again.",
    );
    expect(latest.error).not.toContain("max_subscriptions");
    expect(latest.isPending).toBe(false);
    expect(latest.isReady).toBe(false);
  });
});
