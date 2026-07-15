/**
 * Route constants and query-string builders for app navigation targets.
 */
export const APP_ROUTE_PATHS = {
  dashboard: "/",
  knowledge: "/knowledge",
  reports: "/reports",
  settings: "/settings",
  profile: "/profile",
  usage: "/usage",
} as const;

export const ROUTE_QUERY_KEYS = {
  dashboardWorkspace: "workspace",
  knowledgeTab: "tab",
  reportsTab: "tab",
  settingsSection: "section",
  profileTab: "tab",
} as const;

function buildPathWithQuery(path: string, query: Record<string, string | null | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value) {
      params.set(key, value);
    }
  });
  const serialized = params.toString();
  return serialized ? `${path}?${serialized}` : path;
}

export function buildDashboardWorkspacePath(workspaceId: string): string {
  return buildPathWithQuery(APP_ROUTE_PATHS.dashboard, {
    [ROUTE_QUERY_KEYS.dashboardWorkspace]: workspaceId,
  });
}

export function buildKnowledgeTabPath(tabId: string): string {
  return buildPathWithQuery(APP_ROUTE_PATHS.knowledge, {
    [ROUTE_QUERY_KEYS.knowledgeTab]: tabId,
  });
}

export function buildReportsTabPath(tabId: string): string {
  return buildPathWithQuery(APP_ROUTE_PATHS.reports, {
    [ROUTE_QUERY_KEYS.reportsTab]: tabId,
  });
}

export function buildSettingsSectionPath(sectionId: string): string {
  return buildPathWithQuery(APP_ROUTE_PATHS.settings, {
    [ROUTE_QUERY_KEYS.settingsSection]: sectionId,
  });
}

export function buildProfileTabPath(tabId: string): string {
  return buildPathWithQuery(APP_ROUTE_PATHS.profile, {
    [ROUTE_QUERY_KEYS.profileTab]: tabId,
  });
}

export function readAllowedQueryValue<T extends string>(
  location: string,
  queryKey: string,
  allowedValues: readonly T[],
  fallback: T,
): T {
  const search = location.includes("?")
    ? location.slice(location.indexOf("?"))
    : typeof window === "undefined"
      ? ""
      : window.location.search;
  const value = new URLSearchParams(search).get(queryKey);
  return value && allowedValues.includes(value as T) ? (value as T) : fallback;
}
