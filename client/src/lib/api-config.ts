/**
 * Universal API Configuration System
 * Automatically detects deployment environment and configures API endpoints
 * Supports development, cloud, and enterprise on-premise deployments
 */

import {
  getAccessToken,
  invalidateSessionAndRedirect,
  recoverSessionAfterAuthFailure,
} from "./auth-session";
import {
  ACTIVE_TENANT_HEADER,
  applyActiveTenantHeader,
  isTenantContextResettableError,
  resetStoredActiveTenantContext,
} from "./tenant-context";

export function clearAuthSessionAndRedirect(): void {
  invalidateSessionAndRedirect();
}

export interface ApiConfig {
  baseUrl: string;
  timeout: number;
  retries: number;
  environment: 'development' | 'cloud' | 'onpremise';
  debug: boolean;
}

function isOnPremiseHostname(hostname: string): boolean {
  return (
    hostname.includes(".local") ||
    hostname.includes(".corp") ||
    hostname.includes(".internal") ||
    /^\d+\.\d+\.\d+\.\d+$/.test(hostname)
  );
}

/**
 * Resolve API base URL for on-prem / lab hosts (IP, .local, etc.).
 * Docker/nginx deployments expose `/api` on the same origin (port 80/443).
 */
export function resolveOnPremiseApiBaseUrl(
  hostname: string,
  protocol: string,
  port: string,
): string {
  const isStandardWebPort = !port || port === "80" || port === "443";
  if (isStandardWebPort) {
    return "";
  }
  if (port === "3000" || port === "5000") {
    return `${protocol}//${hostname}:8000`;
  }
  return `${protocol}//${hostname}:${port}`;
}

/**
 * Detects the current deployment environment and generates appropriate API configuration
 */
function detectEnvironment(): ApiConfig {
  const hostname = window.location.hostname;
  const protocol = window.location.protocol;
  const port = window.location.port;
  const isDev = Boolean(import.meta.env.DEV);
  const allowCrossOriginDevApi = String(import.meta.env.VITE_ALLOW_CROSS_ORIGIN_DEV_API || "").toLowerCase() === "true";
  const sameOriginApi = String(import.meta.env.VITE_SAME_ORIGIN_API || "").toLowerCase() === "true";

  // Check for explicit environment variable override
  const explicitApiUrl = import.meta.env.VITE_API_URL;
  if (explicitApiUrl) {
    if (isDev && !allowCrossOriginDevApi) {
      return {
        baseUrl: "",
        timeout: parseInt(import.meta.env.VITE_API_TIMEOUT || "120000"),
        retries: 3,
        environment: (import.meta.env.VITE_ENVIRONMENT as any) || "development",
        debug: true,
      };
    }
    return {
      baseUrl: explicitApiUrl,
      timeout: parseInt(import.meta.env.VITE_API_TIMEOUT || '30000'),
      retries: 3,
      environment: (import.meta.env.VITE_ENVIRONMENT as any) || 'development',
      debug: import.meta.env.DEV || false
    };
  }

  // Production Docker/nginx: UI and /api share one origin (management plane, execution-site lab UI).
  if (sameOriginApi) {
    return {
      baseUrl: "",
      timeout: parseInt(import.meta.env.VITE_API_TIMEOUT || "25000"),
      retries: 2,
      environment: (import.meta.env.VITE_ENVIRONMENT as ApiConfig["environment"]) || "onpremise",
      debug: false,
    };
  }

  // Local development detection
  if (hostname === 'localhost' || hostname === '127.0.0.1') {
    return {
      baseUrl: '',
      timeout: 120000, // 2 minutes - pentesting tools can take a while
      retries: 2,
      environment: 'development',
      debug: true
    };
  }

  // Cloud deployment detection (drowai.com domains)
  if (hostname.includes('drowai.com')) {
    const apiHostname = hostname.replace('app.', 'api.');
    return {
      baseUrl: `${protocol}//${apiHostname}`,
      timeout: 20000,
      retries: 3,
      environment: 'cloud',
      debug: false
    };
  }

  // Enterprise on-premise detection (IP, .local, etc.)
  if (isOnPremiseHostname(hostname)) {
    return {
      baseUrl: resolveOnPremiseApiBaseUrl(hostname, protocol, port),
      timeout: 25000,
      retries: 2,
      environment: "onpremise",
      debug: false,
    };
  }

  // Default fallback - assume same-origin with /api prefix
  return {
    baseUrl: `${protocol}//${hostname}${port ? `:${port}` : ''}`,
    timeout: 15000,
    retries: 3,
    environment: 'development',
    debug: import.meta.env.DEV || false
  };
}

// Global API configuration
export const apiConfig = detectEnvironment();

// Debug logging for development environments
if (apiConfig.debug) {
  // eslint-disable-next-line no-console
  console.info('API config:', {
    environment: apiConfig.environment,
    baseUrl: apiConfig.baseUrl,
    hostname: window.location.hostname,
    timeout: apiConfig.timeout
  });
}

type ComposedAbortSignal = {
  signal: AbortSignal;
  cleanup: () => void;
};

function composeAbortSignals(signals: Array<AbortSignal | null | undefined>): ComposedAbortSignal | null {
  const activeSignals = signals.filter((signal): signal is AbortSignal => signal != null);
  if (activeSignals.length === 0) {
    return null;
  }
  if (activeSignals.length === 1) {
    return {
      signal: activeSignals[0],
      cleanup: () => {},
    };
  }

  const abortSignalAny = (AbortSignal as unknown as { any?: (sources: AbortSignal[]) => AbortSignal }).any;
  if (typeof abortSignalAny === "function") {
    return {
      signal: abortSignalAny(activeSignals),
      cleanup: () => {},
    };
  }

  const controller = new AbortController();
  const listeners = new Map<AbortSignal, EventListener>();

  const forwardAbort = (source: AbortSignal) => {
    if (controller.signal.aborted) {
      return;
    }
    controller.abort(source.reason);
  };

  for (const signal of activeSignals) {
    if (signal.aborted) {
      forwardAbort(signal);
      continue;
    }
    const listener: EventListener = () => {
      forwardAbort(signal);
    };
    listeners.set(signal, listener);
    signal.addEventListener("abort", listener, { once: true });
  }

  const cleanup = () => {
    for (const [signal, listener] of listeners.entries()) {
      signal.removeEventListener("abort", listener);
    }
    listeners.clear();
  };

  if (controller.signal.aborted) {
    cleanup();
  }

  return {
    signal: controller.signal,
    cleanup,
  };
}

/**
 * Enhanced fetch function with authentication, retries, and error handling
 */
export async function apiFetch(
  endpoint: string,
  options: RequestInit = {}
): Promise<Response> {
  const normalizedEndpoint = endpoint.startsWith("/") ? endpoint : `/${endpoint}`;
  const url = endpoint.startsWith('http') 
    ? endpoint 
    : `${apiConfig.baseUrl}${endpoint.startsWith('/') ? endpoint : `/${endpoint}`}`;

  const method = (options.method ?? "GET").toUpperCase();
  const isAuthLoginOrRegisterEndpoint =
    normalizedEndpoint.includes("/api/auth/login") || normalizedEndpoint.includes("/api/auth/register");
  const isAuthRefreshEndpoint = normalizedEndpoint.includes("/api/auth/refresh");

  let lastError: Error = new Error('Unknown error');
  let didAuthRecoveryForRequest = false;
  let didTenantContextRecoveryForRequest = false;

  // Retry logic
  for (let attempt = 1; attempt <= apiConfig.retries; attempt++) {
    try {
      const headers = new Headers(options.headers ?? {});
      const token = getAccessToken();
      if (token && !headers.has("Authorization")) {
        headers.set("Authorization", `Bearer ${token}`);
      }
      applyActiveTenantHeader(headers);
      const hasBody = options.body !== undefined && options.body !== null;
      const isFormDataBody = typeof FormData !== "undefined" && options.body instanceof FormData;
      if (hasBody && !isFormDataBody && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
      if (!hasBody && (method === "GET" || method === "HEAD")) {
        headers.delete("Content-Type");
      }

      // Configure request with timeout for each attempt
      const timeoutController = new AbortController();
      const timeoutId = setTimeout(() => timeoutController.abort(), apiConfig.timeout);
      const composedSignal = composeAbortSignals([options.signal, timeoutController.signal]);

      const requestOptions: RequestInit = {
        ...options,
        headers,
        signal: composedSignal?.signal ?? timeoutController.signal
      };

      if (apiConfig.debug) {
        // eslint-disable-next-line no-console
        console.info(`API Request: ${options.method || 'GET'} ${url}`);
      }

      const response = await fetch(url, requestOptions).finally(() => {
        clearTimeout(timeoutId);
        composedSignal?.cleanup();
      });

      if (apiConfig.debug) {
        // eslint-disable-next-line no-console
        console.info(`API Response: ${response.status} ${response.statusText}`);
      }

      if (response.status === 401) {
        const hasAuthHeader = headers.has("Authorization");
        const canTryRecovery =
          hasAuthHeader &&
          !didAuthRecoveryForRequest &&
          !isAuthLoginOrRegisterEndpoint &&
          !isAuthRefreshEndpoint;

        if (canTryRecovery) {
          didAuthRecoveryForRequest = true;
          const recovered = await recoverSessionAfterAuthFailure({
            source: "http_401",
            endpoint: normalizedEndpoint,
            method,
          });
          if (recovered) {
            continue;
          }
        }
      }

      if (
        !didTenantContextRecoveryForRequest &&
        headers.has("Authorization") &&
        headers.has(ACTIVE_TENANT_HEADER) &&
        isTenantContextResettableError(
          response.status,
          await response
            .clone()
            .json()
            .catch(() => null),
        )
      ) {
        didTenantContextRecoveryForRequest = true;
        resetStoredActiveTenantContext();
        continue;
      }

      return response;
    } catch (error) {
      lastError = error as Error;
      const callerAborted = Boolean(options.signal?.aborted);
      
      // Handle abort errors specifically
      if (lastError.name === 'AbortError') {
        if (!callerAborted) {
          lastError = new Error(`Request timeout after ${apiConfig.timeout}ms`);
        }
      }
      
      if (apiConfig.debug) {
        console.warn(`❌ API Request failed (attempt ${attempt}/${apiConfig.retries}):`, {
          error: lastError.message,
          name: lastError.name,
          url: url,
          method: options.method || 'GET'
        });
      }

      // Don't retry on authentication errors, timeouts, or client errors
      if (lastError.message.includes('Authentication required') || 
          lastError.message.includes('timeout') ||
          lastError.name === 'AbortError' ||
          lastError.message.includes('400') ||
          lastError.message.includes('401') ||
          lastError.message.includes('403') ||
          lastError.message.includes('404')) {
        break;
      }

      // Wait before retry with exponential backoff and jitter
      if (attempt < apiConfig.retries) {
        const baseDelay = Math.min(1000 * Math.pow(2, attempt), 5000);
        const jitter = Math.random() * 1000;
        await new Promise(resolve => setTimeout(resolve, baseDelay + jitter));
      }
    }
  }

  throw lastError;
}

/**
 * Convenience methods for different HTTP verbs
 */
export const apiRequest = {
  get: (endpoint: string) => apiFetch(endpoint, { method: 'GET' }),
  
  post: (endpoint: string, data?: any) => apiFetch(endpoint, {
    method: 'POST',
    body: data ? JSON.stringify(data) : undefined
  }),
  
  put: (endpoint: string, data?: any) => apiFetch(endpoint, {
    method: 'PUT',
    body: data ? JSON.stringify(data) : undefined
  }),
  
  patch: (endpoint: string, data?: any) => apiFetch(endpoint, {
    method: 'PATCH',
    body: data ? JSON.stringify(data) : undefined
  }),
  
  delete: (endpoint: string) => apiFetch(endpoint, { method: 'DELETE' })
};

/**
 * Helper function for API calls that return JSON
 */
export async function apiCall<T = any>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  const response = await apiFetch(endpoint, options);
  
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`API Error ${response.status}: ${errorText}`);
  }
  
  return response.json();
}
