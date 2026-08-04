/**
 * Browser-history helpers for account pages opened from the user menu.
 *
 * Responsibility: mark app-initiated Profile and Settings entries, preserve
 * that marker across same-page tabs, and provide a safe app-local fallback for
 * direct entries.
 */
import { APP_ROUTE_PATHS } from "@/navigation/routes";

const ACCOUNT_PAGE_IN_APP_ORIGIN_KEY = "__drowaiAccountPageInAppOrigin";

type NavigationOptions = {
  replace?: boolean;
  state?: unknown;
};

export type AppNavigate = (target: string, options?: NavigationOptions) => void;

function createInAppAccountPageState(): Record<string, boolean> {
  return { [ACCOUNT_PAGE_IN_APP_ORIGIN_KEY]: true };
}

function accountPagePath(location: string): string | null {
  const [pathname] = location.split(/[?#]/, 1);
  if (pathname === APP_ROUTE_PATHS.profile || pathname === APP_ROUTE_PATHS.settings) {
    return pathname;
  }
  return null;
}

export function isAccountPageLocation(location: string): boolean {
  return accountPagePath(location) !== null;
}

export function hasInAppAccountPageOrigin(state: unknown): boolean {
  return (
    typeof state === "object"
    && state !== null
    && ACCOUNT_PAGE_IN_APP_ORIGIN_KEY in state
    && (state as Record<string, unknown>)[ACCOUNT_PAGE_IN_APP_ORIGIN_KEY] === true
  );
}

export function navigateToAccountPage(
  navigate: AppNavigate,
  target: string,
  currentLocation: string,
  currentHistoryState: unknown = window.history.state,
): void {
  const currentAccountPage = accountPagePath(currentLocation);
  const targetAccountPage = accountPagePath(target);
  const staysOnSameAccountPage = (
    currentAccountPage !== null
    && currentAccountPage === targetAccountPage
  );

  navigate(target, {
    replace: staysOnSameAccountPage,
    state: staysOnSameAccountPage
      ? currentHistoryState
      : createInAppAccountPageState(),
  });
}

export function replaceAccountPageLocation(
  navigate: AppNavigate,
  target: string,
  currentHistoryState: unknown = window.history.state,
): void {
  navigate(target, {
    replace: true,
    state: currentHistoryState,
  });
}

export function returnFromAccountPage(
  navigate: AppNavigate,
  currentHistoryState: unknown = window.history.state,
): void {
  if (hasInAppAccountPageOrigin(currentHistoryState)) {
    window.history.back();
    return;
  }

  navigate(APP_ROUTE_PATHS.dashboard, { replace: true });
}
