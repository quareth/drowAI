/**
 * Tenant context hook/provider for frontend active-tenant state.
 *
 * Responsibilities:
 * - derive tenant context from authenticated `/api/auth/me` payload
 * - persist validated active-tenant selection for HTTP/WS hint propagation
 * - expose tenant-switch action for multi-tenant users
 */

import { createContext, ReactNode, useContext, useEffect, useMemo } from "react";
import { useMutation } from "@tanstack/react-query";

import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/hooks/use-auth";
import { apiFetch } from "@/lib/api-config";
import { queryClient } from "@/lib/queryClient";
import {
  dispatchActiveTenantChanged,
  clearStoredActiveTenantId,
  getStoredActiveTenantId,
  onActiveTenantChanged,
  setStoredActiveTenantId,
} from "@/lib/tenant-context";
import { clearTenantScopedQueryCaches } from "@/lib/tenant-query-cache";

export interface ActiveTenantContext {
  tenant_id: number;
  membership_id: number;
  role: string;
  is_default_tenant: boolean;
  source: string;
}

export interface TenantMembershipSummary {
  membership_id: number;
  tenant_id: number;
  tenant_slug: string;
  tenant_name: string;
  role: string;
  membership_status: string;
  tenant_status: string;
  is_default_tenant: boolean;
}

export interface EffectivePermissions {
  actions: string[];
  role: string;
  tenant_id: number;
  policy_version: string;
}

interface TenantContextPayload {
  active_tenant: ActiveTenantContext | null;
  membership_summaries: TenantMembershipSummary[];
  effective_permissions: EffectivePermissions | null;
}

interface TenantContextValue {
  activeTenant: ActiveTenantContext | null;
  membershipSummaries: TenantMembershipSummary[];
  effectivePermissions: EffectivePermissions | null;
  isLoading: boolean;
  isMultiTenant: boolean;
  isSwitchingTenant: boolean;
  switchTenant: (tenantId: number) => Promise<void>;
}

const TenantContext = createContext<TenantContextValue | null>(null);

function isActiveMembership(summary: TenantMembershipSummary): boolean {
  return (
    String(summary.membership_status).toLowerCase() === "active" &&
    String(summary.tenant_status).toLowerCase() === "active"
  );
}

function normalizeTenantId(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return Math.floor(value);
  }
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null;
  }
  return Math.floor(parsed);
}

export function TenantContextProvider({ children }: { children: ReactNode }) {
  const { toast } = useToast();
  const { user, isLoading } = useAuth();

  const membershipSummaries = useMemo(() => user?.membership_summaries ?? [], [user]);
  const activeMemberships = useMemo(
    () => membershipSummaries.filter((membership) => isActiveMembership(membership)),
    [membershipSummaries],
  );
  const activeTenant = user?.active_tenant ?? null;
  const effectivePermissions = user?.effective_permissions ?? null;
  const isMultiTenant = activeMemberships.length > 1;

  useEffect(() => {
    return onActiveTenantChanged((detail) => {
      clearTenantScopedQueryCaches(queryClient);
      if (detail.nextTenantId !== null) {
        return;
      }

      queryClient.setQueryData(["/api/auth/me"], (previous: unknown) => {
        if (!previous || typeof previous !== "object") {
          return previous;
        }
        return {
          ...(previous as Record<string, unknown>),
          active_tenant: null,
          effective_permissions: null,
        };
      });
      void queryClient.invalidateQueries({ queryKey: ["/api/auth/me"] });
    });
  }, []);

  useEffect(() => {
    if (isLoading) {
      return;
    }

    if (!user) {
      clearStoredActiveTenantId();
      return;
    }

    if (activeMemberships.length === 0) {
      clearStoredActiveTenantId();
      return;
    }

    const validTenantIds = new Set(activeMemberships.map((membership) => Number(membership.tenant_id)));
    const currentActiveTenantId = normalizeTenantId(activeTenant?.tenant_id);
    if (currentActiveTenantId !== null && validTenantIds.has(currentActiveTenantId)) {
      setStoredActiveTenantId(currentActiveTenantId);
      return;
    }

    const storedTenantId = getStoredActiveTenantId();
    if (storedTenantId !== null && validTenantIds.has(storedTenantId)) {
      return;
    }

    clearStoredActiveTenantId();
  }, [activeMemberships, activeTenant?.tenant_id, isLoading, user]);

  const switchTenantMutation = useMutation({
    mutationFn: async (tenantId: number) => {
      const normalizedTenantId = normalizeTenantId(tenantId);
      if (normalizedTenantId === null) {
        throw new Error("Invalid tenant id");
      }

      const response = await apiFetch("/api/tenants/context/switch", {
        method: "POST",
        body: JSON.stringify({ tenant_id: normalizedTenantId }),
      });
      if (!response.ok) {
        const errorPayload = await response.json().catch(() => null);
        const detail =
          errorPayload && typeof errorPayload === "object" && "detail" in errorPayload
            ? String((errorPayload as { detail?: unknown }).detail ?? "")
            : "";
        throw new Error(detail || "Failed to switch tenant");
      }

      return (await response.json()) as TenantContextPayload;
    },
    onSuccess: (payload) => {
      const previousTenantId = getStoredActiveTenantId();
      const nextTenantId = normalizeTenantId(payload.active_tenant?.tenant_id);
      if (nextTenantId !== null) {
        setStoredActiveTenantId(nextTenantId);
      } else {
        clearStoredActiveTenantId();
      }

      if (previousTenantId !== nextTenantId) {
        clearTenantScopedQueryCaches(queryClient);
        dispatchActiveTenantChanged({
          previousTenantId,
          nextTenantId,
        });
      }

      queryClient.setQueryData(["/api/auth/me"], (previous: unknown) => {
        if (!previous || typeof previous !== "object") {
          return previous;
        }
        const previousRecord = previous as Record<string, unknown>;
        return {
          ...previousRecord,
          active_tenant: payload.active_tenant,
          membership_summaries: payload.membership_summaries,
          effective_permissions: payload.effective_permissions,
        };
      });
      queryClient.invalidateQueries({ queryKey: ["/api/auth/me"] });
    },
    onError: (error: Error) => {
      toast({
        title: "Tenant switch failed",
        description: error.message,
        variant: "destructive",
      });
    },
  });

  const contextValue = useMemo<TenantContextValue>(
    () => ({
      activeTenant,
      membershipSummaries: activeMemberships,
      effectivePermissions,
      isLoading,
      isMultiTenant,
      isSwitchingTenant: switchTenantMutation.isPending,
      switchTenant: async (tenantId: number) => {
        await switchTenantMutation.mutateAsync(tenantId);
      },
    }),
    [
      activeMemberships,
      activeTenant,
      effectivePermissions,
      isLoading,
      isMultiTenant,
      switchTenantMutation,
    ],
  );

  return <TenantContext.Provider value={contextValue}>{children}</TenantContext.Provider>;
}

export function useTenantContext() {
  const context = useContext(TenantContext);
  if (!context) {
    throw new Error("useTenantContext must be used within a TenantContextProvider");
  }
  return context;
}
