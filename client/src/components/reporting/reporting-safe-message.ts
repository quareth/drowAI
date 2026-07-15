/**
 * Safe display helpers for reporting workflow backend messages.
 *
 * Responsibility: centralize lightweight redaction and raw-payload suppression
 * for reporting UI status, action, and progress components.
 */

export function safeReportingMessage(
  message: string | null | undefined,
  rawPayloadFallback: string,
): string | null {
  const trimmed = message?.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return rawPayloadFallback;
  }
  return trimmed
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer <redacted>")
    .replace(/\b(token|api[_-]?key|password|secret)=\S+/gi, "$1=<redacted>");
}
