/**
 * Shared heading and contextual return control for user-menu account pages.
 */
import { ArrowLeft } from "lucide-react";
import { useLocation } from "wouter";

import { Button } from "@/components/ui/button";
import { returnFromAccountPage } from "@/navigation/account-page-history";

interface AccountPageHeaderProps {
  title: string;
  description: string;
}

export function AccountPageHeader({ title, description }: AccountPageHeaderProps) {
  const [, setLocation] = useLocation();

  return (
    <header className="mb-8 flex items-start gap-3">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Go back"
        onClick={() => returnFromAccountPage(setLocation)}
        className="mt-0.5 shrink-0 text-gray-300 hover:bg-slate-800 hover:text-white"
      >
        <ArrowLeft aria-hidden="true" />
      </Button>
      <div>
        <h1 className="mb-2 text-3xl font-bold text-white">{title}</h1>
        <p className="text-gray-400">{description}</p>
      </div>
    </header>
  );
}
