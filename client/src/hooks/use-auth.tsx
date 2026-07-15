/**
 * Auth provider hook for user session lifecycle.
 *
 * Responsibilities:
 * - hold authenticated user profile state via `/api/auth/me`
 * - coordinate login/register/logout mutations
 * - register centralized auth recovery for token refresh and tenant-safe `/me` retries
 */

import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { UseMutationResult, useMutation, useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { apiConfig } from "@/lib/api-config";
import {
  ACCESS_TOKEN_REFRESH_SKEW_MS,
  addAccessTokenChangeListener,
  clearAccessToken,
  getAccessTokenExpiresAtMs,
  getAccessToken,
  registerAuthRecoveryHandler,
  setAccessToken,
} from "@/lib/auth-session";
import {
  applyActiveTenantHeader,
  isTenantContextResettableError,
  resetStoredActiveTenantContext,
} from "@/lib/tenant-context";
import { useToast } from "@/hooks/use-toast";
import { apiRequest, getQueryFn, queryClient } from "../lib/queryClient";

interface ActiveTenantContext {
  tenant_id: number;
  membership_id: number;
  role: string;
  is_default_tenant: boolean;
  source: string;
}

interface TenantMembershipSummary {
  membership_id: number;
  tenant_id: number;
  tenant_slug: string;
  tenant_name: string;
  role: string;
  membership_status: string;
  tenant_status: string;
  is_default_tenant: boolean;
}

interface EffectivePermissions {
  actions: string[];
  role: string;
  tenant_id: number;
  policy_version: string;
}

// Define types locally to match FastAPI backend.
interface User {
  id: number;
  username: string;
  email?: string;
  created_at: string;
  is_active: boolean;
  active_tenant?: ActiveTenantContext | null;
  membership_summaries?: TenantMembershipSummary[];
  effective_permissions?: EffectivePermissions | null;
}

interface InsertUser {
  username: string;
  password: string;
  email?: string;
}

const insertUserSchema = z.object({
  username: z.string().min(1, "Username is required"),
  password: z.string().min(6, "Password must be at least 6 characters"),
  email: z.string().email("Invalid email").optional(),
});
void insertUserSchema;

type AuthContextType = {
  user: User | null;
  isLoading: boolean;
  error: Error | null;
  loginMutation: UseMutationResult<User, Error, LoginData>;
  logoutMutation: UseMutationResult<void, Error, void>;
  registerMutation: UseMutationResult<User, Error, InsertUser>;
};

type LoginData = Pick<InsertUser, "username" | "password">;

export const AuthContext = createContext<AuthContextType | null>(null);

function resolveApiUrl(endpoint: string): string {
  if (endpoint.startsWith("http")) {
    return endpoint;
  }
  const normalizedEndpoint = endpoint.startsWith("/") ? endpoint : `/${endpoint}`;
  return `${apiConfig.baseUrl}${normalizedEndpoint}`;
}

function buildTenantAwareAuthHeaders(token: string): Headers {
  const headers = new Headers({
    Authorization: `Bearer ${token}`,
  });
  applyActiveTenantHeader(headers);
  return headers;
}

async function fetchMeWithTenantRecovery(url: string, token: string): Promise<Response> {
  let response = await fetch(url, {
    method: "GET",
    headers: buildTenantAwareAuthHeaders(token),
  });

  const shouldResetTenantHint = isTenantContextResettableError(
    response.status,
    await response
      .clone()
      .json()
      .catch(() => null),
  );

  if (shouldResetTenantHint) {
    resetStoredActiveTenantContext();
    response = await fetch(url, {
      method: "GET",
      headers: buildTenantAwareAuthHeaders(token),
    });
  }

  return response;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const { toast } = useToast();
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [hasToken, setHasToken] = useState(() => {
    if (typeof window === "undefined") {
      return false;
    }
    return !!getAccessToken();
  });
  const [tokenVersion, setTokenVersion] = useState(0);

  const refreshSessionFromCookie = useCallback(async () => {
    const refreshUrl = resolveApiUrl("/api/auth/refresh");
    const refreshResponse = await fetch(refreshUrl, {
      method: "POST",
      credentials: "include",
    });
    if (!refreshResponse.ok) {
      setHasToken(false);
      return false;
    }

    const refreshPayload = await refreshResponse.json().catch(() => null as unknown);
    const refreshedToken =
      refreshPayload &&
      typeof refreshPayload === "object" &&
      "access_token" in refreshPayload &&
      typeof (refreshPayload as { access_token?: unknown }).access_token === "string"
        ? ((refreshPayload as { access_token: string }).access_token)
        : null;
    if (!refreshedToken) {
      setHasToken(false);
      return false;
    }

    setAccessToken(refreshedToken);
    setHasToken(true);
    setTokenVersion((current) => current + 1);

    const meUrl = resolveApiUrl("/api/auth/me");
    const refreshedMeResponse = await fetchMeWithTenantRecovery(meUrl, refreshedToken);
    if (!refreshedMeResponse.ok) {
      setHasToken(false);
      return false;
    }

    const refreshedUser = await refreshedMeResponse.json();
    queryClient.setQueryData(["/api/auth/me"], refreshedUser);
    return true;
  }, []);

  useEffect(() => {
    const checkToken = () => {
      setHasToken(!!getAccessToken());
      setTokenVersion((current) => current + 1);
    };

    checkToken();
    return addAccessTokenChangeListener(checkToken);
  }, []);

  useEffect(() => {
    const recoverSession = async () => {
      const existingToken = getAccessToken();
      if (!existingToken) {
        setHasToken(false);
        return false;
      }

      return refreshSessionFromCookie();
    };

    registerAuthRecoveryHandler(recoverSession);
    return () => {
      registerAuthRecoveryHandler(null);
    };
  }, [refreshSessionFromCookie]);

  useEffect(() => {
    if (refreshTimerRef.current) {
      clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
    if (!hasToken) {
      return;
    }
    const expiresAtMs = getAccessTokenExpiresAtMs();
    if (expiresAtMs === null) {
      return;
    }
    const delayMs = Math.max(0, expiresAtMs - Date.now() - ACCESS_TOKEN_REFRESH_SKEW_MS);
    refreshTimerRef.current = setTimeout(() => {
      refreshTimerRef.current = null;
      void refreshSessionFromCookie();
    }, delayMs);

    return () => {
      if (refreshTimerRef.current) {
        clearTimeout(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
    };
  }, [hasToken, refreshSessionFromCookie, tokenVersion]);

  const { data: user, error, isLoading } = useQuery<User | undefined, Error>({
    queryKey: ["/api/auth/me"],
    queryFn: getQueryFn({ on401: "returnNull" }),
    enabled: hasToken,
    // Keep tenant-scoped permissions fresh after role/membership changes.
    staleTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: hasToken ? 60_000 : false,
    refetchIntervalInBackground: false,
  });

  const loginMutation = useMutation({
    mutationFn: async (credentials: LoginData) => {
      try {
        const res = await apiRequest("POST", "/api/auth/login", credentials);
        if (!res.ok) {
          const errorData = await res.json();
          throw new Error(errorData.detail || "Login failed");
        }
        const data = await res.json();
        setAccessToken(data.access_token);
        setTokenVersion((current) => current + 1);
        return data.user;
      } catch (error) {
        if (error instanceof Error) {
          throw error;
        }
        throw new Error("Login failed");
      }
    },
    onSuccess: (nextUser: User) => {
      resetStoredActiveTenantContext();
      setHasToken(true);
      queryClient.setQueryData(["/api/auth/me"], nextUser);
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => {
      toast({
        title: "Login failed",
        description: error.message,
        variant: "destructive",
      });
    },
  });

  const registerMutation = useMutation({
    mutationFn: async (credentials: InsertUser) => {
      try {
        const res = await apiRequest("POST", "/api/auth/register", credentials);
        if (!res.ok) {
          const errorData = await res.json();
          throw new Error(errorData.detail || "Registration failed");
        }
        const data = await res.json();
        setAccessToken(data.access_token);
        setTokenVersion((current) => current + 1);
        return data.user;
      } catch (error) {
        if (error instanceof Error) {
          throw error;
        }
        throw new Error("Registration failed");
      }
    },
    onSuccess: (nextUser: User) => {
      resetStoredActiveTenantContext();
      setHasToken(true);
      queryClient.setQueryData(["/api/auth/me"], nextUser);
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => {
      toast({
        title: "Registration failed",
        description: error.message,
        variant: "destructive",
      });
    },
  });

  const logoutMutation = useMutation({
    mutationFn: async () => {
      try {
        await apiRequest("POST", "/api/auth/logout");
      } catch {
        // Best effort logout endpoint for stateless JWT setup.
      }
      clearAccessToken();
      resetStoredActiveTenantContext();
    },
    onSuccess: () => {
      setHasToken(false);
      queryClient.setQueryData(["/api/auth/me"], null);
      queryClient.clear();
      window.location.href = "/auth";
    },
    onError: (error: Error) => {
      toast({
        title: "Logout failed",
        description: error.message,
        variant: "destructive",
      });
    },
  });

  return (
    <AuthContext.Provider
      value={{
        user: user ?? null,
        isLoading,
        error,
        loginMutation,
        logoutMutation,
        registerMutation,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
