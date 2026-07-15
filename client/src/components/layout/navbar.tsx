/**
 * Primary application navbar with account and tenant context controls.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useAuth } from "@/hooks/use-auth";
import { useTenantContext } from "@/hooks/use-tenant-context";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { 
  DropdownMenu, 
  DropdownMenuContent, 
  DropdownMenuItem, 
  DropdownMenuTrigger,
  DropdownMenuSeparator 
} from "@/components/ui/dropdown-menu";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Search, ChevronDown, Settings, User, LogOut, BookOpen } from "lucide-react";
import { useLocation } from "wouter";
import { DrowLogo } from "@/components/ui/drow-logo";
import { NotificationMenu } from "@/components/layout/notification-menu";
import { APP_ROUTE_PATHS } from "@/navigation/routes";
import { useAppDestinationSearch } from "@/navigation/use-app-destination-search";
import type { SearchMatch } from "@/navigation/types";

const NAVBAR_SEARCH_PLACEHOLDER = "Search";
const SEARCH_RESULT_LIST_ID = "navbar-search-results";
const DOCS_URL = "https://drowai.com/user-guide";

export function Navbar() {
  const { user, logoutMutation } = useAuth();
  const {
    activeTenant,
    effectivePermissions,
    isMultiTenant,
    isSwitchingTenant,
    membershipSummaries,
    switchTenant,
  } = useTenantContext();
  const [, setLocation] = useLocation();
  const searchRootRef = useRef<HTMLDivElement | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  const [activeResultIndex, setActiveResultIndex] = useState(0);
  const resultGroups = useAppDestinationSearch(searchQuery, effectivePermissions?.actions);
  const flattenedResults = useMemo<SearchMatch[]>(
    () => resultGroups.flatMap((group) => group.matches),
    [resultGroups],
  );
  const hasSearchQuery = searchQuery.trim().length > 0;
  const hasSearchResults = flattenedResults.length > 0;

  useEffect(() => {
    setActiveResultIndex(0);
  }, [searchQuery]);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      if (!searchRootRef.current?.contains(event.target as Node)) {
        setIsSearchOpen(false);
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  const handleLogout = () => {
    logoutMutation.mutate();
  };

  const handleSettings = () => {
    setLocation(APP_ROUTE_PATHS.settings);
  };

  const handleProfile = () => {
    setLocation(APP_ROUTE_PATHS.profile);
  };

  const handleDocs = () => {
    window.open(DOCS_URL, "_blank", "noopener,noreferrer");
  };

  const openSearchResult = (match: SearchMatch) => {
    setLocation(match.destination.href);
    setSearchQuery("");
    setIsSearchOpen(false);
  };

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      setIsSearchOpen(false);
      return;
    }

    if (!hasSearchResults) {
      return;
    }

    if (event.key === "ArrowDown") {
      event.preventDefault();
      setIsSearchOpen(true);
      setActiveResultIndex((current) => (current + 1) % flattenedResults.length);
      return;
    }

    if (event.key === "ArrowUp") {
      event.preventDefault();
      setIsSearchOpen(true);
      setActiveResultIndex((current) =>
        current === 0 ? flattenedResults.length - 1 : current - 1,
      );
      return;
    }

    if (event.key === "Enter") {
      event.preventDefault();
      openSearchResult(flattenedResults[activeResultIndex] ?? flattenedResults[0]);
    }
  };

  const selectedTenantValue = activeTenant ? String(activeTenant.tenant_id) : "";
  const shouldShowSearchPanel = isSearchOpen && hasSearchQuery;

  return (
    <nav className="bg-slate-900 border-b border-slate-700 px-4 py-2 flex items-center justify-between relative z-50">
      {/* Left Section */}
      <div className="flex items-center space-x-4">
        <div className="flex items-center space-x-2">
          <div className="w-8 h-8 flex items-center justify-center">
            <DrowLogo size={32} />
          </div>
          <span className="text-xl font-bold bg-gradient-to-r from-blue-400 to-purple-400 bg-clip-text text-transparent">
            DrowAI
          </span>
        </div>
        <div className="h-6 w-px bg-slate-600"></div>
        <div className="text-sm text-gray-400">Red Team Platform</div>
      </div>

      {/* Center Search */}
      <div ref={searchRootRef} className="flex-1 max-w-md mx-8">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400 w-4 h-4" />
          <Input
            type="text"
            value={searchQuery}
            onChange={(event) => {
              setSearchQuery(event.target.value);
              setIsSearchOpen(true);
            }}
            onFocus={() => setIsSearchOpen(true)}
            onKeyDown={handleSearchKeyDown}
            placeholder={NAVBAR_SEARCH_PLACEHOLDER}
            role="combobox"
            aria-expanded={shouldShowSearchPanel}
            aria-controls={SEARCH_RESULT_LIST_ID}
            aria-autocomplete="list"
            className="w-full bg-slate-800 border-slate-600 rounded-lg pl-10 pr-4 py-2 text-sm focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors text-white placeholder:text-gray-400"
          />
          {shouldShowSearchPanel ? (
            <div
              id={SEARCH_RESULT_LIST_ID}
              role="listbox"
              className="absolute left-0 right-0 top-full z-50 mt-2 max-h-96 overflow-auto rounded-lg border border-slate-700 bg-slate-950 py-2 shadow-xl"
            >
              {hasSearchResults ? (
                resultGroups.map((group) => (
                  <div key={group.group} className="px-2 py-1">
                    <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                      {group.group}
                    </div>
                    <div className="space-y-1">
                      {group.matches.map((match) => {
                        const flatIndex = flattenedResults.findIndex(
                          (candidate) => candidate.destination.id === match.destination.id,
                        );
                        const isActive = flatIndex === activeResultIndex;
                        const Icon = match.destination.icon;
                        return (
                          <button
                            key={match.destination.id}
                            type="button"
                            role="option"
                            aria-selected={isActive}
                            className={`flex w-full items-center gap-2 rounded-md px-2 py-2 text-left transition-colors ${
                              isActive
                                ? "bg-blue-600/20 text-white"
                                : "text-slate-200 hover:bg-slate-800"
                            }`}
                            onMouseEnter={() => setActiveResultIndex(flatIndex)}
                            onClick={() => openSearchResult(match)}
                          >
                            {Icon ? <Icon className="h-4 w-4 shrink-0 text-slate-400" /> : null}
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-sm font-medium">
                                {match.destination.label}
                              </span>
                              {match.destination.description ? (
                                <span className="block truncate text-xs text-slate-400">
                                  {match.destination.description}
                                </span>
                              ) : null}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ))
              ) : (
                <div className="px-4 py-3 text-sm text-slate-400">No destinations found</div>
              )}
            </div>
          ) : null}
        </div>
      </div>

      {/* Right Section */}
      <div className="flex items-center space-x-4">
        {isMultiTenant && (
          <div className="flex items-center gap-2 min-w-[220px]">
            <span className="text-xs uppercase tracking-wide text-slate-400">Tenant</span>
            <Select
              value={selectedTenantValue}
              onValueChange={(value) => {
                const parsed = Number.parseInt(value, 10);
                if (!Number.isFinite(parsed) || parsed <= 0) {
                  return;
                }
                void switchTenant(parsed);
              }}
              disabled={isSwitchingTenant}
            >
              <SelectTrigger
                aria-label="Tenant"
                className="h-8 min-w-[170px] bg-slate-800 border-slate-600 text-white"
              >
                <SelectValue placeholder="Select tenant" />
              </SelectTrigger>
              <SelectContent>
                {membershipSummaries.map((membership) => (
                  <SelectItem key={membership.membership_id} value={String(membership.tenant_id)}>
                    {membership.tenant_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}

        <NotificationMenu />
        
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" className="flex items-center space-x-2 p-2">
              <Avatar className="w-6 h-6">
                <AvatarFallback className="bg-gradient-to-br from-green-500 to-blue-500 text-white text-xs">
                  {user?.username?.charAt(0).toUpperCase() || 'U'}
                </AvatarFallback>
              </Avatar>
              <span className="text-sm font-medium text-white">{user?.username}</span>
              <ChevronDown className="w-3 h-3 text-gray-400" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuItem onClick={handleProfile} className="cursor-pointer">
              <User className="w-4 h-4 mr-2" />
              Profile
            </DropdownMenuItem>
            <DropdownMenuItem onClick={handleSettings} className="cursor-pointer">
              <Settings className="w-4 h-4 mr-2" />
              Settings
            </DropdownMenuItem>
            <DropdownMenuItem onClick={handleDocs} className="cursor-pointer">
              <BookOpen className="w-4 h-4 mr-2" />
              Docs
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onClick={handleLogout}
              className="cursor-pointer text-red-400 focus:text-red-300"
            >
              <LogOut className="w-4 h-4 mr-2" />
              Logout
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </nav>
  );
}
