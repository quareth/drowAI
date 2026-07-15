/**
 * Settings page shell and section routing.
 *
 * Responsibility: render account/application settings sections inside the
 * standard app shell and keep section tabs synchronized with URL query state.
 */
import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useLocation } from "wouter";

import { Navbar } from "@/components/layout/navbar";
import { Sidebar } from "@/components/layout/sidebar";
import { ApiSettingsPanel } from "@/components/settings/api-settings-panel";
import { CveSettingsPanel } from "@/components/settings/cve-settings-panel";
import { DataManagementSettingsPanel } from "@/components/settings/data-management-settings-panel";
import { DisplaySettingsPanel } from "@/components/settings/display-settings-panel";
import { NetworkSettingsPanel } from "@/components/settings/network-settings-panel";
import { RunnerSitesPanel } from "@/components/settings/runner-sites-panel";
import { SystemSettingsPanel } from "@/components/settings/system-settings-panel";
import {
  DEFAULT_SETTINGS_SECTION,
  SETTINGS_SECTIONS,
  type SettingsSectionId,
} from "@/components/settings/settings-navigation";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/hooks/use-auth";
import { API_SETTINGS_QUERY_KEY } from "@/hooks/use-api-settings";
import { CVE_SETTINGS_CONFIG_QUERY_KEY, CVE_SETTINGS_STATUS_QUERY_KEY } from "@/hooks/use-cve-settings";
import { DATA_MANAGEMENT_SETTINGS_QUERY_KEY } from "@/hooks/use-data-management-settings";
import { buildSettingsSectionPath, readAllowedQueryValue, ROUTE_QUERY_KEYS } from "@/navigation/routes";

const SETTINGS_SECTION_IDS = SETTINGS_SECTIONS.map((section) => section.id);

export default function SettingsPage() {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [location, setLocation] = useLocation();
  const locationSection = readAllowedQueryValue(
    location,
    ROUTE_QUERY_KEYS.settingsSection,
    SETTINGS_SECTION_IDS,
    DEFAULT_SETTINGS_SECTION,
  );
  const [activeSection, setActiveSection] = useState<SettingsSectionId>(locationSection);

  useEffect(() => {
    setActiveSection(locationSection);
  }, [locationSection]);

  const handleSectionChange = (sectionId: string) => {
    const nextSection = sectionId as SettingsSectionId;
    setActiveSection(nextSection);
    setLocation(buildSettingsSectionPath(nextSection));
  };

  useEffect(() => {
    if (!user) {
      return;
    }
    void queryClient.prefetchQuery({ queryKey: API_SETTINGS_QUERY_KEY });
    void queryClient.prefetchQuery({ queryKey: CVE_SETTINGS_CONFIG_QUERY_KEY });
    void queryClient.prefetchQuery({ queryKey: CVE_SETTINGS_STATUS_QUERY_KEY });
    void queryClient.prefetchQuery({ queryKey: DATA_MANAGEMENT_SETTINGS_QUERY_KEY });
  }, [queryClient, user]);

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      <Navbar />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <div className="flex-1 p-6 overflow-auto">
          <div className="flex items-center justify-between mb-8">
            <div className="flex items-center space-x-4">
              <div>
                <h1 className="text-3xl font-bold text-white mb-2">Settings</h1>
                <p className="text-gray-400">Manage your account and application preferences</p>
              </div>
            </div>
          </div>

          <Tabs
            value={activeSection}
            onValueChange={handleSectionChange}
            className="space-y-6"
          >
            <TabsList className="bg-slate-800 border border-slate-700">
              {SETTINGS_SECTIONS.map((section) => {
                const Icon = section.icon;
                return (
                  <TabsTrigger key={section.id} value={section.id} className="data-[state=active]:bg-blue-600">
                    <Icon className="w-4 h-4 mr-2" />
                    {section.label}
                  </TabsTrigger>
                );
              })}
            </TabsList>

            <TabsContent value="api" className="space-y-6">
              <ApiSettingsPanel queryEnabled={Boolean(user)} />
            </TabsContent>

            <TabsContent value="network" className="space-y-6">
              <NetworkSettingsPanel />
            </TabsContent>

            <TabsContent value="runner-sites" className="space-y-6">
              <RunnerSitesPanel />
            </TabsContent>

            <TabsContent value="system" className="space-y-6">
              <SystemSettingsPanel />
            </TabsContent>

            <TabsContent value="data-management" className="space-y-6">
              <DataManagementSettingsPanel queryEnabled={Boolean(user)} />
            </TabsContent>

            <TabsContent value="display" className="space-y-6">
              <DisplaySettingsPanel queryEnabled={Boolean(user)} />
            </TabsContent>

            <TabsContent value="cve" className="space-y-6">
              <CveSettingsPanel />
            </TabsContent>
          </Tabs>
        </div>
      </div>
    </div>
  );
}
