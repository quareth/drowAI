/* Shared presentation helpers for engagement service socket labels. */

export interface ServicePresentationInput {
  id?: string | null;
  service_key?: string | null;
  service_name?: string | null;
  application_protocol?: string | null;
  protocol?: string | null;
  transport_protocol?: string | null;
  port?: number | null;
  metadata?: Record<string, unknown> | null;
}

const WEB_SURFACE_APPLICATION_PROTOCOLS = new Set(["http", "https"]);

export function formatServiceDisplayName(service: ServicePresentationInput): string {
  const serviceName = normalizeDisplayToken(service.service_name);
  if (serviceName) {
    return serviceName;
  }
  const socket = formatServiceSocket(service);
  return socket || service.service_key || service.id || "Unknown service";
}

export function formatServiceIdentityLabel(service: ServicePresentationInput): string | null {
  const key = String(service.service_key || "").trim();
  if (key) {
    return key;
  }
  return null;
}

export function formatServiceSocket(service: ServicePresentationInput): string | null {
  const protocol = String(service.protocol || service.transport_protocol || "").trim().toUpperCase();
  const port = service.port ?? null;
  if (!protocol && port === null) {
    return null;
  }
  return `${protocol || "SERVICE"} ${port ?? "-"}`;
}

export function isWebSurfaceService(service: ServicePresentationInput): boolean {
  for (const candidate of serviceProtocolCandidates(service)) {
    const normalized = normalizeProtocolToken(candidate);
    if (normalized && WEB_SURFACE_APPLICATION_PROTOCOLS.has(normalized)) {
      return true;
    }
  }
  return false;
}

function serviceProtocolCandidates(service: ServicePresentationInput): unknown[] {
  const metadata = service.metadata || {};
  const state =
    typeof metadata.state === "object" && metadata.state !== null
      ? (metadata.state as Record<string, unknown>)
      : {};
  return [
    service.application_protocol,
    service.service_name,
    service.protocol,
    metadata.application_protocol,
    metadata.service_name,
    metadata.protocol,
    metadata.scheme,
    state.application_protocol,
    state.service_name,
    state.protocol,
    state.scheme,
  ];
}

function normalizeProtocolToken(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  return normalized || null;
}

function normalizeDisplayToken(value: string | null | undefined): string | null {
  const normalized = String(value || "").trim();
  if (!normalized) {
    return null;
  }
  if (/^[a-z0-9_.+-]{2,8}$/i.test(normalized)) {
    return normalized.toUpperCase();
  }
  return normalized;
}
