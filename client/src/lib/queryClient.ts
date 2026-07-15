import { QueryClient, QueryFunction } from "@tanstack/react-query";
import { apiFetch } from "./api-config";
import { getAccessToken } from "./auth-session";

// Legacy compatibility wrapper for existing code
export function apiRequest(method: string, url: string, data?: unknown): Promise<Response>;
export function apiRequest(url: string, options?: RequestInit): Promise<unknown>;
export async function apiRequest(
  methodOrUrl: string,
  urlOrOptions?: string | RequestInit,
  data?: unknown,
): Promise<Response | unknown> {
  const isMethodStyle = typeof urlOrOptions === "string";
  if (isMethodStyle) {
    return apiFetch(urlOrOptions, {
      method: methodOrUrl,
      body: data ? JSON.stringify(data) : undefined,
    });
  }

  const response = await apiFetch(methodOrUrl, urlOrOptions);
  if (!response.ok) {
    const errorText = await response.text().catch(() => `HTTP ${response.status}`);
    throw new Error(`${response.status}: ${errorText}`);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

type UnauthorizedBehavior = "returnNull" | "throw";
export const getQueryFn: <T>(options: {
  on401: UnauthorizedBehavior;
}) => QueryFunction<T> =
  ({ on401: unauthorizedBehavior }) =>
  async ({ queryKey, signal }) => {
    const url = queryKey[0] as string;
    
    try {
      const res = await apiFetch(url, { 
        method: 'GET',
        signal 
      });

      if (unauthorizedBehavior === "returnNull" && res.status === 401) {
        return null;
      }

      if (!res.ok) {
        let errorText: string;
        try {
          const contentType = res.headers.get('content-type');
          if (contentType?.includes('application/json')) {
            const errorData = await res.json();
            errorText = errorData.detail || errorData.message || `HTTP ${res.status}`;
          } else {
            errorText = await res.text() || `HTTP ${res.status}`;
          }
        } catch {
          errorText = `HTTP ${res.status} - ${res.statusText}`;
        }
        throw new Error(`${res.status}: ${errorText}`);
      }

      const contentType = res.headers.get('content-type');
      if (contentType?.includes('application/json')) {
        return await res.json();
      } else {
        return await res.text();
      }
    } catch (error) {
      if (signal?.aborted) {
        throw new Error('Request was cancelled');
      }
      
      console.error('Query fetch failed:', {
        url,
        error: error instanceof Error ? error.message : 'Unknown error',
        timestamp: new Date().toISOString()
      });
      
      if (unauthorizedBehavior === "returnNull" && error instanceof Error && error.message.includes('401')) {
        return null;
      }
      throw error;
    }
  };

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      queryFn: getQueryFn({ on401: "throw" }),
      refetchInterval: false,
      refetchOnWindowFocus: false,
      staleTime: 5 * 60 * 1000, // 5 minutes
      retry: (failureCount, error) => {
        // Don't retry on auth errors or if no token is available
        if (!getAccessToken()) return false;
        if (error instanceof Error && error.message.includes('401')) return false;
        // Retry up to 2 times for network errors
        return failureCount < 2;
      },
      retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30000),
    },
    mutations: {
      retry: (failureCount, error) => {
        // Don't retry mutations on auth errors
        if (error instanceof Error && error.message.includes('401')) return false;
        // Retry once for network errors
        return failureCount < 1;
      },
    },
  },
});
