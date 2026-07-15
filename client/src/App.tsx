/**
 * App shell composition for providers and top-level routing.
 */
import { useEffect } from "react";
import { Switch, Route, Redirect } from "wouter";
import { queryClient } from "./lib/queryClient";
import { QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "@/hooks/use-auth";
import { useRuntimeNotifications } from "@/hooks/useRuntimeNotifications";
import { TenantContextProvider } from "@/hooks/use-tenant-context";
import { ProtectedRoute } from "@/lib/protected-route";
import { PlanProvider } from "@/contexts/PlanContext";
import RuntimeStreamBootstrap from "@/components/runtime/RuntimeStreamBootstrap";
import Dashboard from "@/pages/dashboard";
import KnowledgeWorkspacePage from "@/pages/knowledge-workspace-page";
import ReportsPage from "@/pages/reports-page";
import UsagePage from "@/pages/usage-page";
import SettingsPage from "@/pages/settings-page";
import ProfilePage from "@/pages/profile-page";
import AuthPage from "@/pages/auth-page";
import SetupPage from "@/pages/setup";
import SetupGate from "@/components/setup/SetupGate";
import NotFound from "@/pages/not-found";
import { prewarmTopologyLayout } from "@/components/engagements/territory/topology-layout";

function RedirectToKnowledge() {
  return <Redirect to="/knowledge" />;
}

function RuntimeNotificationBridge() {
  useRuntimeNotifications();
  return null;
}

export function Router() {
  return (
    <Switch>
      <Route path="/setup" component={SetupPage} />
      <ProtectedRoute path="/knowledge" component={KnowledgeWorkspacePage} />
      <ProtectedRoute path="/engagements" component={RedirectToKnowledge} />
      <ProtectedRoute path="/engagements/:id" component={RedirectToKnowledge} />
      <ProtectedRoute path="/reports" component={ReportsPage} />
      <ProtectedRoute path="/usage" component={UsagePage} />
      <ProtectedRoute path="/settings" component={SettingsPage} />
      <ProtectedRoute path="/profile" component={ProfilePage} />
      <ProtectedRoute path="/" component={Dashboard} />
      <Route path="/auth" component={AuthPage} />
      <Route path="/login" component={AuthPage} />
      <Route path="/register" component={AuthPage} />
      <Route component={NotFound} />
    </Switch>
  );
}

function App() {
  useEffect(() => {
    prewarmTopologyLayout();
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <TenantContextProvider>
          <TooltipProvider>
            <PlanProvider>
              <RuntimeStreamBootstrap />
              <RuntimeNotificationBridge />
              <div className="dark min-h-screen bg-slate-950">
                <Toaster />
                <SetupGate>
                  <Router />
                </SetupGate>
              </div>
            </PlanProvider>
          </TooltipProvider>
        </TenantContextProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}

export default App;
