/**
 * Shared Settings mutation helpers to keep error/session handling DRY.
 */
import type { ToastActionElement } from "@/components/ui/toast";

import { Button } from "@/components/ui/button";

export const SESSION_EXPIRED_MESSAGE = "Your session has expired. Please log in again.";

type ToastFn = (payload: {
  title: string;
  description?: string;
  variant?: "default" | "destructive";
  action?: ToastActionElement;
}) => void;

export async function toApiError(response: Response, fallbackMessage: string): Promise<Error> {
  let detail = fallbackMessage;
  try {
    const payload = await response.json();
    if (payload && typeof payload === "object") {
      const value = (payload as { detail?: unknown; message?: unknown }).detail
        ?? (payload as { detail?: unknown; message?: unknown }).message;
      if (typeof value === "string" && value.trim()) {
        detail = value;
      }
    }
  } catch {
    // Use fallback when response body is not JSON.
  }
  return new Error(detail);
}

export function showSessionExpiredToast(toast: ToastFn): void {
  toast({
    title: "Session Expired",
    description: "Please log in again to continue.",
    variant: "destructive",
    action: (
      <Button variant="outline" size="sm" onClick={() => { window.location.href = "/auth"; }}>
        Login
      </Button>
    ),
  });
}

