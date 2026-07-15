import { useCallback, useMemo, useState } from "react";
import { ListTodo, Pencil, Trash2, X } from "lucide-react";

import { cn } from "@/lib/utils";
import type { SendQueueApi } from "@/hooks/useSendQueue";

export interface QueueIndicatorProps {
  queue: SendQueueApi;
  className?: string;
  maxPreviewChars?: number;
}

function truncatePreview(text: string, max = 80): string {
  const singleLine = (text || "").split(/\r?\n/)[0] ?? "";
  if (singleLine.length <= max) return singleLine;
  return singleLine.slice(0, max - 1) + "…";
}

export function QueueIndicator({ queue, className, maxPreviewChars = 80 }: QueueIndicatorProps) {
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");

  const count = queue.count;
  const items = queue.items;

  // Removed console.log to prevent excessive re-renders

  const beginEdit = useCallback((id: string) => {
    const item = items.find((i) => i.id === id);
    if (!item) return;
    setEditingId(id);
    setDraft(item.content);
  }, [items]);

  const cancelEdit = useCallback(() => {
    setEditingId(null);
    setDraft("");
  }, []);

  const saveEdit = useCallback(() => {
    if (!editingId) return;
    const value = draft.trim();
    if (!value) {
      // Remove empty edits instead of keeping blanks
      queue.remove(editingId);
    } else {
      queue.modify(editingId, value);
    }
    setEditingId(null);
    setDraft("");
  }, [draft, editingId, queue]);

  const headerLabel = useMemo(() => {
    return count === 1 ? "1 Queued" : `${count} Queued`;
  }, [count]);

  if (count === 0) return null;

  return (
    <div className={cn("relative", className)}>
      <button
        type="button"
        className={cn(
          "inline-flex items-center gap-2 rounded-full border border-indigo-400/60 bg-indigo-500/20 px-3 py-1 text-xs font-medium text-indigo-100",
          "shadow-sm backdrop-blur hover:border-indigo-300 hover:bg-indigo-500/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
        )}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
      >
        <ListTodo className="h-4 w-4" aria-hidden="true" />
        {headerLabel}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Queued"
          className={cn(
            "absolute right-0 z-20 mt-2 w-80 max-w-[85vw] rounded-lg border border-slate-800 bg-slate-900/95 p-2 text-slate-200 shadow-xl",
          )}
        >
          <div className="flex items-center justify-between px-2 py-1">
            <div className="text-xs font-semibold uppercase tracking-wide text-slate-400">Queued</div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
                aria-label="Close"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
          </div>

          <ul className="max-h-72 space-y-2 overflow-y-auto p-1">
            {items.map((item) => {
              const isEditing = editingId === item.id;
              return (
                <li key={item.id} className="rounded-md border border-slate-800 bg-slate-900/70 p-2">
                  {!isEditing ? (
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm text-slate-200">{truncatePreview(item.content, maxPreviewChars)}</div>
                        <div className="text-[11px] text-slate-400">Sends after message finishes</div>
                      </div>
                      <div className="flex flex-shrink-0 items-center gap-1">
                        <button
                          type="button"
                          onClick={() => beginEdit(item.id)}
                          className="inline-flex items-center gap-1 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-[11px] text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
                        >
                          <Pencil className="h-3.5 w-3.5" aria-hidden="true" /> Modify
                        </button>
                        <button
                          type="button"
                          onClick={() => queue.remove(item.id)}
                          className="inline-flex items-center gap-1 rounded border border-rose-700/60 bg-rose-900/30 px-2 py-1 text-[11px] text-rose-200 hover:bg-rose-900/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-500"
                        >
                          <Trash2 className="h-3.5 w-3.5" aria-hidden="true" /> Remove
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="flex flex-col gap-2">
                      <textarea
                        value={draft}
                        onChange={(e) => setDraft(e.target.value.slice(0, 4000))}
                        rows={3}
                        className="w-full resize-none rounded-md border border-slate-700 bg-slate-900/60 p-2 text-sm text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
                        aria-label="Edit queued message"
                        maxLength={4000}
                      />
                      <div className="flex items-center justify-end gap-2">
                        <button
                          type="button"
                          onClick={cancelEdit}
                          className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-[11px] text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-500"
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          onClick={saveEdit}
                          className="rounded border border-emerald-700/60 bg-emerald-900/30 px-2 py-1 text-[11px] text-emerald-200 hover:bg-emerald-900/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
                        >
                          Save
                        </button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

export default QueueIndicator;

