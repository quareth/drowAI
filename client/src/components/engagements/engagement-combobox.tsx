/**
 * Searchable engagement picker: existing engagements, inline create, or none (auto-create).
 * Uses Command + Popover; task counts come from the parent map (not the engagement list API).
 */

import { useMemo, useState } from "react";
import { Check, ChevronsUpDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useCreateEngagement, useEngagements } from "@/hooks/use-engagement-knowledge";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import type { EngagementListItem } from "@/types/engagement-knowledge";

export interface EngagementComboboxProps {
  value: number | null;
  onChange: (engagementId: number | null) => void;
  disabled?: boolean;
  taskCountByEngagement?: Map<number, number>;
  allowCreate?: boolean;
  allowNone?: boolean;
  helperText?: string | null;
  ariaLabel?: string;
}

export function EngagementCombobox({
  value,
  onChange,
  disabled,
  taskCountByEngagement,
  allowCreate = true,
  allowNone = true,
  helperText = "Leave empty to auto-create from task name",
  ariaLabel,
}: EngagementComboboxProps) {
  const [open, setOpen] = useState(false);
  const { toast } = useToast();
  const { data, isLoading } = useEngagements({ limit: 100 });
  const createEngagement = useCreateEngagement();

  const items: EngagementListItem[] = data?.items ?? [];

  const selectedName = useMemo(() => {
    if (value == null) {
      return null;
    }
    return items.find((e) => e.id === value)?.name ?? null;
  }, [items, value]);

  const [search, setSearch] = useState("");

  const trimmed = search.trim();
  const hasExact =
    trimmed.length > 0 &&
    items.some((e) => e.name.toLowerCase() === trimmed.toLowerCase());
  const showCreate = allowCreate && trimmed.length > 0 && !hasExact;
  const emptyText = isLoading
    ? "Loading engagements..."
    : trimmed
      ? "No matching engagements."
      : "No engagements available.";

  return (
    <div className="space-y-1">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="outline"
            role="combobox"
            aria-label={ariaLabel}
            aria-expanded={open}
            disabled={disabled}
            className="w-full justify-between border-slate-600 bg-slate-800 text-left font-normal text-white hover:bg-slate-700"
          >
            <span className="truncate">{selectedName ?? "Search or select…"}</span>
            <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-[var(--radix-popover-trigger-width)] border-slate-700 bg-slate-900 p-0 text-white">
          <Command className="bg-slate-900 text-white" shouldFilter>
            <CommandInput
              placeholder="Search engagements…"
              value={search}
              onValueChange={setSearch}
              className="text-white placeholder:text-slate-500"
            />
            <CommandList>
              <CommandEmpty>{emptyText}</CommandEmpty>
              <CommandGroup heading="Engagements">
                {items.map((e) => {
                  const count = taskCountByEngagement?.get(e.id) ?? 0;
                  return (
                    <CommandItem
                      key={e.id}
                      value={e.name}
                      onSelect={() => {
                        onChange(e.id);
                        setOpen(false);
                        setSearch("");
                      }}
                      className="text-slate-100 aria-selected:bg-slate-800"
                    >
                      <Check className={cn("mr-2 h-4 w-4", value === e.id ? "opacity-100" : "opacity-0")} />
                      <span className="truncate">{e.name}</span>
                      <span className="ml-auto text-xs text-slate-500">({count})</span>
                    </CommandItem>
                  );
                })}
              </CommandGroup>
              {showCreate ? (
                <CommandGroup heading="Create">
                  <CommandItem
                    value={`__create__ ${trimmed}`}
                    disabled={createEngagement.isPending}
                    onSelect={() => {
                      void (async () => {
                        try {
                          const row = await createEngagement.mutateAsync({ name: trimmed });
                          onChange(row.id);
                          setOpen(false);
                          setSearch("");
                          toast({ title: "Engagement created", description: row.name });
                        } catch (err) {
                          toast({
                            title: "Could not create engagement",
                            description: err instanceof Error ? err.message : "Unknown error",
                            variant: "destructive",
                          });
                        }
                      })();
                    }}
                    className="text-emerald-300 aria-selected:bg-slate-800"
                  >
                    + Create &quot;{trimmed}&quot;
                  </CommandItem>
                </CommandGroup>
              ) : null}
              {allowNone ? (
                <>
                  <CommandSeparator className="bg-slate-700" />
                  <CommandGroup>
                    <CommandItem
                      value="__none_auto__"
                      onSelect={() => {
                        onChange(null);
                        setOpen(false);
                        setSearch("");
                      }}
                      className="text-slate-300 aria-selected:bg-slate-800"
                    >
                      None (auto-create from task name)
                    </CommandItem>
                  </CommandGroup>
                </>
              ) : null}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {helperText ? <p className="text-xs text-slate-500">{helperText}</p> : null}
    </div>
  );
}
