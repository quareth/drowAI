/**
 * Markdown export utilities for persisted chat transcripts.
 *
 * Responsibility: fetch complete paginated transcript history, keep only
 * user/assistant messages, and render a local Markdown download payload.
 */
import {
  fetchLatestTranscriptPage,
  fetchOlderTranscriptPage,
  type ChatTranscriptItem,
} from "@/hooks/chat-history-bootstrap";

const TRANSCRIPT_EXPORT_PAGE_LIMIT = 200;

interface ChatTranscriptExportOptions {
  taskId: number;
  taskName?: string | null;
  conversationId?: string | null;
  exportedAt?: Date;
  signal?: AbortSignal;
}

interface ChatTranscriptExportPayload {
  filename: string;
  markdown: string;
  messageCount: number;
}

function isConversationMessage(item: ChatTranscriptItem): boolean {
  return item.kind === "user" || item.kind === "assistant";
}

function metadataSequence(item: ChatTranscriptItem): number {
  const value = item.metadata?.sequence;
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return 0;
}

function metadataTimestamp(item: ChatTranscriptItem): string | null {
  const value = item.metadata?.timestamp;
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function compareTranscriptItems(a: ChatTranscriptItem, b: ChatTranscriptItem): number {
  const turnDifference = a.turn_number - b.turn_number;
  if (turnDifference !== 0) {
    return turnDifference;
  }
  const sequenceDifference = metadataSequence(a) - metadataSequence(b);
  if (sequenceDifference !== 0) {
    return sequenceDifference;
  }
  return a.id.localeCompare(b.id);
}

function safeFilenameSegment(value: string | null | undefined): string {
  return (value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function formatExportedAt(value: Date): string {
  return Number.isNaN(value.getTime()) ? new Date().toISOString() : value.toISOString();
}

function transcriptTitle(taskName: string | null | undefined, taskId: number): string {
  const trimmedName = taskName?.trim();
  return trimmedName ? `${trimmedName} (#${taskId})` : `Task #${taskId}`;
}

export function transcriptDownloadFilename(
  taskId: number,
  taskName?: string | null,
): string {
  const baseName = safeFilenameSegment(taskName) || `task-${taskId}`;
  return `${baseName}-chat-transcript.md`;
}

export function selectConversationTranscriptItems(
  items: ChatTranscriptItem[],
): ChatTranscriptItem[] {
  return items
    .filter(isConversationMessage)
    .slice()
    .sort(compareTranscriptItems);
}

export function renderChatTranscriptMarkdown({
  items,
  taskId,
  taskName,
  conversationId,
  exportedAt = new Date(),
}: Omit<ChatTranscriptExportOptions, "signal"> & { items: ChatTranscriptItem[] }): string {
  const conversationLabel = conversationId?.trim() || "default";
  const lines = [
    "# Chat Transcript",
    "",
    `Task: ${transcriptTitle(taskName, taskId)}`,
    `Conversation: ${conversationLabel}`,
    `Exported: ${formatExportedAt(exportedAt)}`,
    "",
  ];

  for (const item of selectConversationTranscriptItems(items)) {
    const label = item.kind === "user" ? "User" : "Assistant";
    const timestamp = metadataTimestamp(item);
    lines.push(`## ${timestamp ? `${label} - ${timestamp}` : label}`);
    lines.push("");
    lines.push(item.content.trim() || "_No content._");
    lines.push("");
  }

  return lines.join("\n").trimEnd() + "\n";
}

export async function fetchConversationTranscriptItems({
  taskId,
  conversationId,
  signal,
}: Pick<ChatTranscriptExportOptions, "taskId" | "conversationId" | "signal">): Promise<ChatTranscriptItem[]> {
  const pages: ChatTranscriptItem[][] = [];
  let page = await fetchLatestTranscriptPage(taskId, {
    conversationId,
    limit: TRANSCRIPT_EXPORT_PAGE_LIMIT,
    signal,
  });
  pages.unshift(page.items);

  while (
    page.hasMoreOlder
    && typeof page.nextBeforeTurn === "number"
    && page.nextBeforeTurn > 0
  ) {
    page = await fetchOlderTranscriptPage(taskId, {
      conversationId,
      beforeTurn: page.nextBeforeTurn,
      limit: TRANSCRIPT_EXPORT_PAGE_LIMIT,
      signal,
    });
    pages.unshift(page.items);
  }

  return selectConversationTranscriptItems(pages.flat());
}

export async function buildChatTranscriptExport({
  taskId,
  taskName,
  conversationId,
  exportedAt = new Date(),
  signal,
}: ChatTranscriptExportOptions): Promise<ChatTranscriptExportPayload> {
  const items = await fetchConversationTranscriptItems({
    taskId,
    conversationId,
    signal,
  });
  return {
    filename: transcriptDownloadFilename(taskId, taskName),
    markdown: renderChatTranscriptMarkdown({
      items,
      taskId,
      taskName,
      conversationId,
      exportedAt,
    }),
    messageCount: items.length,
  };
}
