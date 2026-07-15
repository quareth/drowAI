/**
 * Tenant context client helpers.
 *
 * Responsibilities:
 * - store and validate active tenant selection in browser-local state
 * - apply the canonical active tenant header to outbound HTTP requests
 * - classify tenant-context errors that require clearing stale tenant hints
 */

export const ACTIVE_TENANT_STORAGE_KEY = "active_tenant_id";
export const ACTIVE_TENANT_HEADER = "X-Active-Tenant-Id";
export const ACTIVE_TENANT_CHANGED_EVENT = "active-tenant-changed";

export interface ActiveTenantChangedDetail {
  previousTenantId: number | null;
  nextTenantId: number | null;
}

function normalizeTenantId(value: unknown): number | null {
  if (value === null || value === undefined) {
    return null;
  }
  const parsed = typeof value === "number" ? value : Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null;
  }
  return Math.floor(parsed);
}

export function getStoredActiveTenantId(): number | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return normalizeTenantId(window.localStorage.getItem(ACTIVE_TENANT_STORAGE_KEY));
  } catch {
    return null;
  }
}

export function setStoredActiveTenantId(tenantId: number): void {
  if (typeof window === "undefined") {
    return;
  }
  const normalized = normalizeTenantId(tenantId);
  if (normalized === null) {
    return;
  }
  window.localStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, String(normalized));
}

export function clearStoredActiveTenantId(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(ACTIVE_TENANT_STORAGE_KEY);
}

export function resetStoredActiveTenantContext(): ActiveTenantChangedDetail {
  const previousTenantId = getStoredActiveTenantId();
  clearStoredActiveTenantId();
  const detail = {
    previousTenantId,
    nextTenantId: null,
  };
  dispatchActiveTenantChanged(detail);
  return detail;
}

export function dispatchActiveTenantChanged(detail: ActiveTenantChangedDetail): void {
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(
    new CustomEvent<ActiveTenantChangedDetail>(ACTIVE_TENANT_CHANGED_EVENT, {
      detail,
    }),
  );
}

export function onActiveTenantChanged(
  listener: (detail: ActiveTenantChangedDetail) => void,
): () => void {
  if (typeof window === "undefined") {
    return () => {};
  }
  const handler = (event: Event) => {
    const detail = (event as CustomEvent<ActiveTenantChangedDetail>).detail;
    listener(detail);
  };
  window.addEventListener(ACTIVE_TENANT_CHANGED_EVENT, handler);
  return () => {
    window.removeEventListener(ACTIVE_TENANT_CHANGED_EVENT, handler);
  };
}

export function applyActiveTenantHeader(headers: Headers): void {
  if (headers.has(ACTIVE_TENANT_HEADER)) {
    return;
  }
  const activeTenantId = getStoredActiveTenantId();
  if (activeTenantId === null) {
    return;
  }
  headers.set(ACTIVE_TENANT_HEADER, String(activeTenantId));
}

export function isTenantContextResettableError(status: number, payload: unknown): boolean {
  if (status !== 400 && status !== 403 && status !== 409) {
    return false;
  }
  const detailValue =
    payload && typeof payload === "object" && "detail" in payload
      ? (payload as { detail?: unknown }).detail
      : null;
  if (typeof detailValue !== "string") {
    return false;
  }
  const detail = detailValue.trim().toLowerCase();
  if (!detail) {
    return false;
  }
  return (
    detail.includes("requested tenant membership is inactive") ||
    detail.includes("requested tenant is not associated with the authenticated user") ||
    detail.includes("must be a positive integer")
  );
}
