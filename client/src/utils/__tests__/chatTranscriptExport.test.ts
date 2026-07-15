/**
 * Verifies Markdown transcript export filtering and history pagination.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatTranscriptItem } from "@/hooks/chat-history-bootstrap";

const mocked = vi.hoisted(() => ({
  fetchLatestTranscriptPage: vi.fn(),
  fetchOlderTranscriptPage: vi.fn(),
}));

vi.mock("@/hooks/chat-history-bootstrap", () => ({
  fetchLatestTranscriptPage: mocked.fetchLatestTranscriptPage,
  fetchOlderTranscriptPage: mocked.fetchOlderTranscriptPage,
}));

import {
  buildChatTranscriptExport,
  renderChatTranscriptMarkdown,
  transcriptDownloadFilename,
} from "@/utils/chatTranscriptExport";

function transcriptItem(
  id: string,
  kind: ChatTranscriptItem["kind"],
  turnNumber: number,
  content: string,
  sequence: number,
): ChatTranscriptItem {
  return {
    id,
    kind,
    turn_number: turnNumber,
    content,
    metadata: {
      sequence,
      timestamp: `2026-06-19T10:0${sequence}:00.000Z`,
    },
  };
}

beforeEach(() => {
  mocked.fetchLatestTranscriptPage.mockReset();
  mocked.fetchOlderTranscriptPage.mockReset();
});

describe("chat transcript export", () => {
  it("renders only user and assistant transcript items", () => {
    const markdown = renderChatTranscriptMarkdown({
      taskId: 119,
      taskName: "HTB",
      conversationId: "conv-a",
      exportedAt: new Date("2026-06-19T11:00:00.000Z"),
      items: [
        transcriptItem("tool-1", "tool", 2, "nmap output", 2),
        transcriptItem("user-1", "user", 1, "Start enumeration", 1),
        transcriptItem("reasoning-1", "reasoning", 2, "private chain", 3),
        transcriptItem("assistant-1", "assistant", 2, "Run service probes", 4),
        transcriptItem("observation-1", "observation", 2, "terminal output", 5),
      ],
    });

    expect(markdown).toContain("# Chat Transcript");
    expect(markdown).toContain("Task: HTB (#119)");
    expect(markdown).toContain("## User - 2026-06-19T10:01:00.000Z");
    expect(markdown).toContain("Start enumeration");
    expect(markdown).toContain("## Assistant - 2026-06-19T10:04:00.000Z");
    expect(markdown).toContain("Run service probes");
    expect(markdown).not.toContain("nmap output");
    expect(markdown).not.toContain("private chain");
    expect(markdown).not.toContain("terminal output");
  });

  it("paginates persisted history before building a transcript export", async () => {
    mocked.fetchLatestTranscriptPage.mockResolvedValueOnce({
      contractVersion: "2026-03-01.chat-history.v2",
      items: [
        transcriptItem("assistant-3", "assistant", 3, "Latest answer", 30),
        transcriptItem("tool-3", "tool", 3, "latest tool output", 31),
      ],
      nextBeforeTurn: 3,
      hasMoreOlder: true,
      startup: null,
    });
    mocked.fetchOlderTranscriptPage.mockResolvedValueOnce({
      contractVersion: "2026-03-01.chat-history.v2",
      items: [
        transcriptItem("user-1", "user", 1, "First question", 10),
        transcriptItem("reasoning-2", "reasoning", 2, "hidden reasoning", 20),
        transcriptItem("assistant-2", "assistant", 2, "Earlier answer", 21),
      ],
      nextBeforeTurn: null,
      hasMoreOlder: false,
      startup: null,
    });

    const exportPayload = await buildChatTranscriptExport({
      taskId: 119,
      taskName: "HTB",
      conversationId: "conv-a",
      exportedAt: new Date("2026-06-19T11:00:00.000Z"),
    });

    expect(mocked.fetchLatestTranscriptPage).toHaveBeenCalledWith(119, {
      conversationId: "conv-a",
      limit: 200,
      signal: undefined,
    });
    expect(mocked.fetchOlderTranscriptPage).toHaveBeenCalledWith(119, {
      conversationId: "conv-a",
      beforeTurn: 3,
      limit: 200,
      signal: undefined,
    });
    expect(exportPayload.filename).toBe("htb-chat-transcript.md");
    expect(exportPayload.messageCount).toBe(3);
    expect(exportPayload.markdown.indexOf("First question")).toBeLessThan(
      exportPayload.markdown.indexOf("Earlier answer"),
    );
    expect(exportPayload.markdown.indexOf("Earlier answer")).toBeLessThan(
      exportPayload.markdown.indexOf("Latest answer"),
    );
    expect(exportPayload.markdown).not.toContain("latest tool output");
    expect(exportPayload.markdown).not.toContain("hidden reasoning");
  });

  it("sanitizes transcript filenames", () => {
    expect(transcriptDownloadFilename(42, "Internal Lab / Prod")).toBe(
      "internal-lab-prod-chat-transcript.md",
    );
    expect(transcriptDownloadFilename(42, "")).toBe("task-42-chat-transcript.md");
  });
});
