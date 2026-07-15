/**
 * Shared date/time formatting utilities for frontend display rendering.
 *
 * Scope:
 * - Formats existing timestamp values for display only.
 * - Uses UTC by default with optional IANA timezone override.
 *
 * Boundary:
 * - No parsing side effects outside conversion to Date.
 * - No API, storage, or application state access.
 */

const DEFAULT_TIMEZONE = "UTC";
const FALLBACK_DISPLAY = "—";

type DateInput = string | number | null | undefined;

function toDate(value: DateInput): Date | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }

  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      return null;
    }

    // Values below 1e12 are treated as epoch seconds, otherwise milliseconds.
    const epochMs = Math.abs(value) < 1e12 ? value * 1000 : value;
    const parsed = new Date(epochMs);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatWithOptions(
  parsed: Date,
  options: Intl.DateTimeFormatOptions,
  timezone: string = DEFAULT_TIMEZONE,
): string {
  return new Intl.DateTimeFormat("en-US", {
    ...options,
    timeZone: timezone,
  }).format(parsed);
}

export function formatDate(
  value: DateInput,
  timezone: string = DEFAULT_TIMEZONE,
): string {
  const parsed = toDate(value);
  if (!parsed) {
    return FALLBACK_DISPLAY;
  }

  return formatWithOptions(parsed, { dateStyle: "medium" }, timezone);
}

export function formatTime(
  value: DateInput,
  timezone: string = DEFAULT_TIMEZONE,
): string {
  const parsed = toDate(value);
  if (!parsed) {
    return FALLBACK_DISPLAY;
  }

  return formatWithOptions(parsed, { hour: "numeric", minute: "2-digit" }, timezone);
}

export function formatDateTime(
  value: DateInput,
  timezone: string = DEFAULT_TIMEZONE,
): string {
  const parsed = toDate(value);
  if (!parsed) {
    return FALLBACK_DISPLAY;
  }

  return formatWithOptions(parsed, { dateStyle: "medium", timeStyle: "short" }, timezone);
}

export function formatRelative(value: DateInput): string {
  const parsed = toDate(value);
  if (!parsed) {
    return FALLBACK_DISPLAY;
  }

  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - parsed.getTime()) / 1000));

  if (elapsedSeconds < 60) {
    return `${elapsedSeconds}s ago`;
  }

  const elapsedMinutes = Math.floor(elapsedSeconds / 60);
  if (elapsedMinutes < 60) {
    return `${elapsedMinutes}m ago`;
  }

  const elapsedHours = Math.floor(elapsedMinutes / 60);
  if (elapsedHours < 24) {
    return `${elapsedHours}h ago`;
  }

  const elapsedDays = Math.floor(elapsedHours / 24);
  if (elapsedDays < 30) {
    return `${elapsedDays}d ago`;
  }

  return formatWithOptions(parsed, { dateStyle: "medium", timeStyle: "short" });
}
