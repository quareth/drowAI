/**
 * Reports page shell.
 *
 * Responsibility: keep the application chrome for `/reports` and delegate all
 * reporting workspace state, data fetching, and workflow controls to the
 * reporting feature components.
 */

import { Navbar } from "@/components/layout/navbar";
import { Sidebar } from "@/components/layout/sidebar";
import { EngagementReportingWorkspace } from "@/components/reporting/engagement-reporting-workspace";
import { ReportLibraryWorkspace } from "@/components/reporting/report-library-workspace";
import {
  WorkspaceTabBar,
} from "@/components/workspace/workspace-tab-bar";
import { buildReportsTabPath, readAllowedQueryValue, ROUTE_QUERY_KEYS } from "@/navigation/routes";
import {
  DEFAULT_REPORTS_TAB,
  REPORTS_TABS,
  type ReportsTabId,
} from "@/pages/reports-navigation";
import { useEffect, useState } from "react";
import { useLocation } from "wouter";

const REPORTS_TAB_IDS = REPORTS_TABS.map((tab) => tab.id);

function resolveReportsTab(location: string): ReportsTabId {
  const search = location.includes("?")
    ? location.slice(location.indexOf("?"))
    : typeof window === "undefined"
      ? ""
      : window.location.search;
  const params = new URLSearchParams(search);
  const requestedTab = params.get(ROUTE_QUERY_KEYS.reportsTab);
  if (requestedTab) {
    return readAllowedQueryValue(
      location,
      ROUTE_QUERY_KEYS.reportsTab,
      REPORTS_TAB_IDS,
      DEFAULT_REPORTS_TAB,
    );
  }
  return params.has("engagement_id") ? "engagement" : DEFAULT_REPORTS_TAB;
}

export default function ReportsPage() {
  const [location, setLocation] = useLocation();
  const locationTab = resolveReportsTab(location);
  const [activeTab, setActiveTab] = useState<ReportsTabId>(locationTab);

  useEffect(() => {
    setActiveTab(locationTab);
  }, [locationTab]);

  const handleTabChange = (tabId: string) => {
    const nextTab = tabId as ReportsTabId;
    setActiveTab(nextTab);
    setLocation(buildReportsTabPath(nextTab));
  };

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      <Navbar />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <div className="flex-1 flex flex-col overflow-hidden">
          <WorkspaceTabBar
            tabs={REPORTS_TABS}
            activeTab={activeTab}
            onTabChange={handleTabChange}
            align="center"
          />
          <div className="flex-1 overflow-hidden">
            {activeTab === "library" ? (
              <ReportLibraryWorkspace />
            ) : (
              <EngagementReportingWorkspace />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
