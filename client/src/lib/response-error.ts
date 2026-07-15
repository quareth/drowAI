/**
 * Shared HTTP response error extraction for API mutations.
 *
 * Responsibilities:
 * - Convert non-OK fetch responses into user-facing Error instances.
 * - Preserve FastAPI `detail`/`message` payloads without duplicating parsers.
 */

export async function responseToError(response: Response, fallbackMessage: string): Promise<Error> {
  const raw = await response.text().catch(() => "");
  if (!raw) {
    return new ApiResponseError(`${fallbackMessage} (${response.status})`, {
      status: response.status,
    });
  }
  try {
    const parsed = JSON.parse(raw) as { detail?: unknown; message?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return new ApiResponseError(parsed.detail.trim(), {
        status: response.status,
        detail: parsed.detail.trim(),
      });
    }
    if (isRecord(parsed.detail)) {
      const message = stringValue(parsed.detail.message) || fallbackMessage;
      return new ApiResponseError(message, {
        status: response.status,
        detail: parsed.detail,
        reasonCode: stringValue(parsed.detail.reason_code),
        reasonCodes: stringArrayValue(parsed.detail.reason_codes),
      });
    }
    if (typeof parsed.message === "string" && parsed.message.trim()) {
      return new ApiResponseError(parsed.message.trim(), {
        status: response.status,
        detail: parsed,
      });
    }
  } catch {
    // Non-JSON response body; fall through to raw text.
  }
  return new ApiResponseError(raw.trim() || `${fallbackMessage} (${response.status})`, {
    status: response.status,
  });
}

export interface ApiResponseErrorOptions {
  status: number;
  detail?: unknown;
  reasonCode?: string | null;
  reasonCodes?: string[];
}

export class ApiResponseError extends Error {
  readonly detail: unknown;
  readonly reasonCode: string | null;
  readonly reasonCodes: string[];
  readonly status: number;

  constructor(message: string, options: ApiResponseErrorOptions) {
    super(message);
    this.name = "ApiResponseError";
    this.status = options.status;
    this.detail = options.detail;
    this.reasonCode = options.reasonCode?.trim() || null;
    this.reasonCodes = normalizeReasonCodes(options.reasonCodes, this.reasonCode);
  }
}

export function getApiErrorReasonCode(error: unknown): string | null {
  return error instanceof ApiResponseError ? error.reasonCode : null;
}

export function getApiErrorReasonCodes(error: unknown): string[] {
  return error instanceof ApiResponseError ? error.reasonCodes : [];
}

function normalizeReasonCodes(reasonCodes: string[] | undefined, reasonCode: string | null): string[] {
  const normalized = (reasonCodes ?? [])
    .map((candidate) => candidate.trim())
    .filter((candidate) => candidate.length > 0);
  if (reasonCode && !normalized.includes(reasonCode)) {
    return [reasonCode, ...normalized];
  }
  return normalized;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function stringArrayValue(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((candidate) => (typeof candidate === "string" ? candidate.trim() : ""))
    .filter((candidate) => candidate.length > 0);
}
