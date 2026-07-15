/**
 * Account profile page.
 *
 * Responsibility: render authenticated account identity, tenant access context,
 * and account security controls inside the standard application shell.
 */
import { useEffect, useState } from "react";
import { Building2, CheckCircle2, Shield, UserRound } from "lucide-react";
import { useLocation } from "wouter";

import { Navbar } from "@/components/layout/navbar";
import { Sidebar } from "@/components/layout/sidebar";
import { PasswordChangeForm } from "@/components/password-change-form";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/hooks/use-auth";
import { useTenantContext } from "@/hooks/use-tenant-context";
import { useUserTimezone } from "@/hooks/use-user-timezone";
import { buildProfileTabPath, readAllowedQueryValue, ROUTE_QUERY_KEYS } from "@/navigation/routes";
import {
  DEFAULT_PROFILE_TAB,
  PROFILE_TABS,
  type ProfileTabId,
} from "@/pages/profile-navigation";
import { formatDate } from "@/utils/datetime";

const PROFILE_TAB_IDS = PROFILE_TABS.map((tab) => tab.id);

function formatRole(value: string | null | undefined): string {
  if (!value) {
    return "Unassigned";
  }
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(" ");
}

function formatStatus(value: string | null | undefined): string {
  return formatRole(value);
}

function AccountField({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-2 break-words text-sm font-medium text-slate-100">{value}</dd>
    </div>
  );
}

export default function ProfilePage() {
  const { user } = useAuth();
  const timezone = useUserTimezone();
  const { activeTenant, membershipSummaries, effectivePermissions } = useTenantContext();
  const [location, setLocation] = useLocation();
  const locationTab = readAllowedQueryValue(
    location,
    ROUTE_QUERY_KEYS.profileTab,
    PROFILE_TAB_IDS,
    DEFAULT_PROFILE_TAB,
  );
  const [activeTab, setActiveTab] = useState<ProfileTabId>(locationTab);

  useEffect(() => {
    setActiveTab(locationTab);
  }, [locationTab]);

  const handleTabChange = (tabId: string) => {
    const nextTab = tabId as ProfileTabId;
    setActiveTab(nextTab);
    setLocation(buildProfileTabPath(nextTab));
  };

  const username = user?.username ?? "Account";
  const initials = username.charAt(0).toUpperCase() || "A";
  const accountStatus = user?.is_active ? "Active" : "Inactive";
  const effectiveRole = effectivePermissions?.role ?? activeTenant?.role ?? null;
  const permissionPreview = effectivePermissions?.actions.slice(0, 6) ?? [];
  const remainingPermissionCount = Math.max(
    0,
    (effectivePermissions?.actions.length ?? 0) - permissionPreview.length,
  );

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      <Navbar />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <main className="flex-1 overflow-auto p-6">
          <div className="mb-8">
            <h1 className="mb-2 text-3xl font-bold text-white">Profile</h1>
            <p className="text-gray-400">Manage account identity, access context, and security controls.</p>
          </div>

          <Tabs
            value={activeTab}
            onValueChange={handleTabChange}
            className="space-y-6"
          >
            <TabsList className="bg-slate-800 border border-slate-700">
              {PROFILE_TABS.map((tab) => {
                const Icon = tab.icon;
                return (
                  <TabsTrigger key={tab.id} value={tab.id} className="data-[state=active]:bg-blue-600">
                    <Icon className="mr-2 h-4 w-4" />
                    {tab.label}
                  </TabsTrigger>
                );
              })}
            </TabsList>

            <TabsContent value="overview" className="space-y-6">
              <Card className="bg-slate-900 border-slate-700">
                <CardContent className="p-6">
                  <div className="flex flex-col gap-6 md:flex-row md:items-center md:justify-between">
                    <div className="flex min-w-0 items-center gap-4">
                      <Avatar className="h-16 w-16">
                        <AvatarFallback className="bg-slate-800 text-xl font-semibold text-blue-200">
                          {initials}
                        </AvatarFallback>
                      </Avatar>
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-3">
                          <h2 className="truncate text-2xl font-semibold text-white">{username}</h2>
                          <Badge
                            className={
                              user?.is_active
                                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/10"
                                : "border-red-500/30 bg-red-500/10 text-red-300 hover:bg-red-500/10"
                            }
                          >
                            {accountStatus}
                          </Badge>
                        </div>
                        {user?.email && <p className="mt-1 text-sm text-slate-400">{user.email}</p>}
                      </div>
                    </div>

                    {activeTenant && (
                      <div className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-3">
                        <div className="flex items-center gap-2 text-sm font-medium text-slate-100">
                          <Building2 className="h-4 w-4 text-blue-300" />
                          Active Tenant
                        </div>
                        <p className="mt-1 text-sm text-slate-400">
                          #{activeTenant.tenant_id} - {formatRole(activeTenant.role)}
                        </p>
                      </div>
                    )}
                  </div>
                </CardContent>
              </Card>

              <Card className="bg-slate-900 border-slate-700">
                <CardHeader>
                  <CardTitle className="flex items-center text-white">
                    <UserRound className="mr-2 h-5 w-5 text-blue-300" />
                    Account Details
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <dl className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
                    {user?.id != null && <AccountField label="User ID" value={user.id} />}
                    <AccountField label="Username" value={username} />
                    {user?.email && <AccountField label="Email" value={user.email} />}
                    {user?.created_at && (
                      <AccountField label="Joined" value={formatDate(user.created_at, timezone)} />
                    )}
                  </dl>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="access" className="space-y-6">
              <Card className="bg-slate-900 border-slate-700">
                <CardHeader>
                  <CardTitle className="flex items-center text-white">
                    <Shield className="mr-2 h-5 w-5 text-blue-300" />
                    Access Context
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-6">
                  <dl className="grid grid-cols-1 gap-4 md:grid-cols-3">
                    <AccountField label="Effective Role" value={formatRole(effectiveRole)} />
                    <AccountField label="Tenant Memberships" value={membershipSummaries.length} />
                    {effectivePermissions?.policy_version && (
                      <AccountField label="Policy Version" value={effectivePermissions.policy_version} />
                    )}
                  </dl>

                  {activeTenant ? (
                    <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div>
                          <h3 className="font-medium text-white">Current Tenant</h3>
                          <p className="mt-1 text-sm text-slate-400">
                            Tenant #{activeTenant.tenant_id}, membership #{activeTenant.membership_id}
                          </p>
                        </div>
                        <div className="flex flex-wrap gap-2">
                          <Badge className="bg-blue-500/10 text-blue-300 hover:bg-blue-500/10">
                            {formatRole(activeTenant.role)}
                          </Badge>
                          {activeTenant.is_default_tenant && (
                            <Badge className="bg-slate-700 text-slate-200 hover:bg-slate-700">Default</Badge>
                          )}
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 text-sm text-slate-400">
                      No active tenant context is assigned to this session.
                    </div>
                  )}

                  {permissionPreview.length > 0 && (
                    <div>
                      <h3 className="mb-3 font-medium text-white">Effective Permissions</h3>
                      <div className="flex flex-wrap gap-2">
                        {permissionPreview.map((permission) => (
                          <Badge
                            key={permission}
                            className="border-slate-600 bg-slate-800 text-slate-200 hover:bg-slate-800"
                          >
                            {permission}
                          </Badge>
                        ))}
                        {remainingPermissionCount > 0 && (
                          <Badge className="border-slate-600 bg-slate-800 text-slate-400 hover:bg-slate-800">
                            +{remainingPermissionCount} more
                          </Badge>
                        )}
                      </div>
                    </div>
                  )}
                </CardContent>
              </Card>

              <Card className="bg-slate-900 border-slate-700">
                <CardHeader>
                  <CardTitle className="flex items-center text-white">
                    <Building2 className="mr-2 h-5 w-5 text-blue-300" />
                    Tenant Memberships
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {membershipSummaries.length > 0 ? (
                    <div className="divide-y divide-slate-700 rounded-lg border border-slate-700">
                      {membershipSummaries.map((membership) => (
                        <div
                          key={membership.membership_id}
                          className="grid grid-cols-1 gap-4 p-4 md:grid-cols-[minmax(0,1fr)_auto]"
                        >
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <h3 className="truncate font-medium text-white">{membership.tenant_name}</h3>
                              {membership.is_default_tenant && (
                                <Badge className="bg-slate-700 text-slate-200 hover:bg-slate-700">Default</Badge>
                              )}
                            </div>
                            <p className="mt-1 text-sm text-slate-400">
                              {membership.tenant_slug} - tenant #{membership.tenant_id}
                            </p>
                          </div>
                          <div className="flex flex-wrap items-center gap-2 md:justify-end">
                            <Badge className="bg-blue-500/10 text-blue-300 hover:bg-blue-500/10">
                              {formatRole(membership.role)}
                            </Badge>
                            <Badge className="bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/10">
                              <CheckCircle2 className="mr-1 h-3 w-3" />
                              {formatStatus(membership.membership_status)}
                            </Badge>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 text-sm text-slate-400">
                      No tenant memberships are attached to this account.
                    </div>
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="security" className="space-y-6">
              <PasswordChangeForm />
            </TabsContent>
          </Tabs>
        </main>
      </div>
    </div>
  );
}
