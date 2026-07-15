/* Reusable local workspace tab bar for dashboard and engagement workspaces. */

import type { ComponentType, ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface WorkspaceTabBarItem {
  id: string;
  label: string;
  icon?: ComponentType<{ className?: string }>;
}

interface WorkspaceTabBarProps {
  tabs: readonly WorkspaceTabBarItem[];
  activeTab: string;
  onTabChange: (tabId: string) => void;
  actions?: ReactNode;
  align?: "start" | "center";
  className?: string;
  tabListClassName?: string;
  tabButtonClassName?: string;
  activeTabClassName?: string;
  inactiveTabClassName?: string;
}

export function WorkspaceTabBar({
  tabs,
  activeTab,
  onTabChange,
  actions,
  align = "center",
  className,
  tabListClassName,
  tabButtonClassName,
  activeTabClassName,
  inactiveTabClassName,
}: WorkspaceTabBarProps) {
  return (
    <div className={cn("bg-slate-900/30 border-b border-slate-800/30", className)}>
      <div
        className={cn(
          "flex items-center gap-3 px-3",
          align === "center" ? "justify-center" : "justify-between",
        )}
      >
        <div
          className={cn(
            "flex min-w-0 items-center space-x-1 overflow-x-auto",
            align === "start" && !actions && "flex-1 justify-start",
            tabListClassName,
          )}
        >
          {tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => onTabChange(tab.id)}
                className={cn(
                  "flex shrink-0 items-center space-x-1.5 border-b-2 px-3 py-1.5 text-xs font-medium transition-colors",
                  activeTab === tab.id
                    ? cn("border-emerald-500 bg-slate-800/30 text-emerald-400", activeTabClassName)
                    : cn("border-transparent text-slate-400 hover:bg-slate-800/20 hover:text-slate-200", inactiveTabClassName),
                  tabButtonClassName,
                )}
                aria-current={activeTab === tab.id ? "page" : undefined}
              >
                {Icon ? <Icon className="h-3 w-3" /> : null}
                <span>{tab.label}</span>
              </button>
            );
          })}
        </div>
        {actions ? <div className="flex items-center gap-1">{actions}</div> : null}
      </div>
    </div>
  );
}
