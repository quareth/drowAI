/**
 * Central auth session coordinator.
 *
 * Responsibilities:
 * - single source of truth for access token storage
 * - guarded auth-failure recovery orchestration
 * - final session invalidation + redirect policy
 */

export type AuthFailureSource = "http_401" | "runtime_ws";

export interface AuthFailureContext {
  source: AuthFailureSource;
  reason?: string;
  endpoint?: string;
  method?: string;
}

type RecoveryHandler = (context: AuthFailureContext) => Promise<boolean>;

const ACCESS_TOKEN_KEY = "access_token";
const ACCESS_TOKEN_CHANGED_EVENT = "drowai-auth-token-changed";
const AUTH_BYPASS_PATHS = ["/auth", "/login", "/register", "/settings"];
export const ACCESS_TOKEN_REFRESH_SKEW_MS = 60_000;

let recoveryHandler: RecoveryHandler | null = null;
let inFlightRecovery: Promise<boolean> | null = null;

function shouldRedirectToAuth(pathname: string): boolean {
  return !AUTH_BYPASS_PATHS.some((prefix) => pathname.includes(prefix));
}

export function getAccessToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function setAccessToken(token: string): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(ACCESS_TOKEN_KEY, token);
  window.dispatchEvent(new Event(ACCESS_TOKEN_CHANGED_EVENT));
}

export function clearAccessToken(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(ACCESS_TOKEN_KEY);
  window.dispatchEvent(new Event(ACCESS_TOKEN_CHANGED_EVENT));
}

export function addAccessTokenChangeListener(listener: () => void): () => void {
  if (typeof window === "undefined") {
    return () => {};
  }
  window.addEventListener("storage", listener);
  window.addEventListener(ACCESS_TOKEN_CHANGED_EVENT, listener);
  return () => {
    window.removeEventListener("storage", listener);
    window.removeEventListener(ACCESS_TOKEN_CHANGED_EVENT, listener);
  };
}

function decodeBase64UrlJson(value: string): unknown {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  return JSON.parse(window.atob(padded));
}

export function getAccessTokenExpiresAtMs(token: string | null = getAccessToken()): number | null {
  if (typeof window === "undefined" || !token) {
    return null;
  }
  const parts = token.split(".");
  if (parts.length < 2) {
    return null;
  }
  try {
    const payload = decodeBase64UrlJson(parts[1]);
    if (!payload || typeof payload !== "object") {
      return null;
    }
    const exp = (payload as { exp?: unknown }).exp;
    if (typeof exp !== "number" || !Number.isFinite(exp) || exp <= 0) {
      return null;
    }
    return Math.floor(exp * 1000);
  } catch {
    return null;
  }
}

export function invalidateSessionAndRedirect(): void {
  if (typeof window === "undefined") {
    return;
  }
  clearAccessToken();
  if (shouldRedirectToAuth(window.location.pathname)) {
    window.location.href = "/auth";
  }
}

export function registerAuthRecoveryHandler(handler: RecoveryHandler | null): void {
  recoveryHandler = handler;
}

export async function recoverSessionAfterAuthFailure(context: AuthFailureContext): Promise<boolean> {
  if (!recoveryHandler) {
    invalidateSessionAndRedirect();
    return false;
  }

  if (!inFlightRecovery) {
    inFlightRecovery = (async () => {
      try {
        return await recoveryHandler(context);
      } catch {
        return false;
      } finally {
        inFlightRecovery = null;
      }
    })();
  }

  const recovered = await inFlightRecovery;
  if (!recovered) {
    invalidateSessionAndRedirect();
  }
  return recovered;
}
