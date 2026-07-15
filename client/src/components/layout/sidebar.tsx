/* Collapsible navigation sidebar: collapsed by default with a slim indicator rail, expands on hover like a drawer. */

import { useLocation } from "wouter";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import {
  Tent,
  Brain,
  FileText,
  Gauge,
  ChevronRight,
} from "lucide-react";

const navigation = [
  { name: "Outpost", icon: Tent, href: "/" },
  { name: "Knowledge", icon: Brain, href: "/knowledge" },
  { name: "Reports", icon: FileText, href: "/reports" },
  { name: "Usage", icon: Gauge, href: "/usage" },
];

export function Sidebar() {
  const [location, setLocation] = useLocation();

  const handleNavigation = (href: string) => {
    setTimeout(() => {
      setLocation(href);
    }, 50);
  };

  return (
    <TooltipProvider delayDuration={300}>
      <aside
        className={cn(
          "group/sidebar flex h-full w-4 shrink-0 flex-col overflow-hidden border-r border-slate-700 bg-slate-900 transition-[width] duration-200 ease-out hover:w-14"
        )}
      >
        <div className="flex min-w-14 flex-1">
          {/* Indicator rail – visible when collapsed, collapses to 0 when expanded so no divider line */}
          <div
            className="flex w-4 shrink-0 flex-col items-center justify-center overflow-hidden border-r border-slate-700/60 py-4 transition-[width] duration-200 ease-out group-hover/sidebar:w-0 group-hover/sidebar:min-w-0 group-hover/sidebar:border-transparent"
            aria-hidden
          >
            <ChevronRight className="h-4 w-4 shrink-0 text-slate-500 transition-colors duration-200 group-hover/sidebar:text-slate-400" />
          </div>

          {/* Drawer content – original icon-only nav, full width when expanded */}
          <div className="flex min-w-0 flex-1 flex-col items-center justify-center py-4">
            <nav className="space-y-3">
              {navigation.map((item) => {
                const isActive =
                  item.href === "/"
                    ? location === "/"
                    : location === item.href || location.startsWith(`${item.href}/`);
                return (
                  <Tooltip key={item.name}>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => handleNavigation(item.href)}
                        aria-label={item.name}
                        className={cn(
                          "w-10 h-10 rounded-lg transition-colors",
                          isActive
                            ? "bg-blue-600 text-white hover:bg-blue-700"
                            : "text-gray-400 hover:bg-slate-800 hover:text-white"
                        )}
                      >
                        <item.icon className="w-5 h-5" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent
                      side="right"
                      className="bg-slate-800 text-white border-slate-700"
                    >
                      {item.name}
                    </TooltipContent>
                  </Tooltip>
                );
              })}
            </nav>
          </div>
        </div>
      </aside>
    </TooltipProvider>
  );
}
