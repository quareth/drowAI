/**
 * Redirects standalone installs to the setup wizard until installation completes.
 */
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { Loader2 } from "lucide-react";

import { fetchSetupStatus } from "@/components/setup/setup-api";
import type { SetupStatus } from "@/components/setup/setup-types";

interface SetupGateProps {
  children: React.ReactNode;
}

export function SetupGate({ children }: SetupGateProps) {
  const [location, setLocation] = useLocation();

  const { data: setupStatus, isLoading } = useQuery<SetupStatus>({
    queryKey: ["/api/setup/status"],
    queryFn: fetchSetupStatus,
    refetchOnWindowFocus: true,
  });

  useEffect(() => {
    if (isLoading || !setupStatus) {
      return;
    }
    if (location === "/setup") {
      // SetupPage owns navigation away from this route. In particular, it must
      // keep the completion result visible until the user chooses Sign in.
      return;
    }
    if (setupStatus.setup_required && setupStatus.wizard_enabled) {
      setLocation("/setup");
    }
  }, [isLoading, location, setLocation, setupStatus]);

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-950 text-slate-100">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    );
  }

  return <>{children}</>;
}

export default SetupGate;
