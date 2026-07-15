/**
 * Provides the authenticated user's timezone preference.
 *
 * Scope:
 * - Reads timezone from the user settings React Query cache.
 * - Returns "UTC" as fallback while loading or when not authenticated.
 *
 * Boundary:
 * - Read-only; does not mutate settings.
 * - No direct API calls; delegates to useApiSettings.
 */
import { useApiSettings } from "@/hooks/use-api-settings";
import { useAuth } from "@/hooks/use-auth";

export const DEFAULT_TIMEZONE = "UTC";

export function useUserTimezone(): string {
  const { user } = useAuth();
  const { settingsQuery } = useApiSettings({ enabled: Boolean(user) });
  return settingsQuery.data?.timezone ?? DEFAULT_TIMEZONE;
}
