/**
 * Multi-file download confirmation dialog for the task workspace file explorer.
 * It shows selected files, total size, and a confirm action for ZIP download.
 */
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";

export interface MultiFileDownloadItem {
  path: string;
  name: string;
  size: number | null;
}

interface MultiFileDownloadDialogProps {
  open: boolean;
  files: MultiFileDownloadItem[];
  isDownloading: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

function formatFileSize(size: number | null): string {
  if (size == null) return "Unknown size";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTotalSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function MultiFileDownloadDialog({
  open,
  files,
  isDownloading,
  onCancel,
  onConfirm,
}: MultiFileDownloadDialogProps) {
  const totalKnownBytes = files.reduce((sum, file) => sum + (file.size ?? 0), 0);
  const unknownSizeCount = files.filter((file) => file.size == null).length;

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && !isDownloading) {
          onCancel();
        }
      }}
    >
      <DialogContent className="max-w-xl border-slate-700 bg-slate-900 text-slate-100">
        <DialogHeader>
          <DialogTitle>Download selected files</DialogTitle>
          <DialogDescription className="text-slate-300">
            {files.length} file{files.length === 1 ? "" : "s"} will be packaged into a ZIP archive.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="rounded border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-300">
            <span className="font-medium text-slate-100">Total size:</span> {formatTotalSize(totalKnownBytes)}
            {unknownSizeCount > 0 ? ` + ${unknownSizeCount} unknown` : ""}
          </div>

          <ScrollArea className="h-56 rounded border border-slate-700 bg-slate-950 p-2">
            <ul className="space-y-1.5">
              {files.map((file) => (
                <li
                  key={file.path}
                  className="flex items-center justify-between gap-2 rounded border border-slate-800 bg-slate-900 px-2.5 py-1.5 text-xs"
                >
                  <span className="truncate text-slate-200" title={file.path}>
                    {file.path}
                  </span>
                  <span className="shrink-0 text-slate-400">{formatFileSize(file.size)}</span>
                </li>
              ))}
            </ul>
          </ScrollArea>

          {isDownloading ? (
            <div className="flex items-center gap-2 text-sm text-slate-300">
              <Loader2 className="h-4 w-4 animate-spin" />
              Preparing zip archive...
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            className="border-slate-600 bg-slate-800 text-slate-100 hover:bg-slate-700"
            onClick={onCancel}
            disabled={isDownloading}
          >
            Cancel
          </Button>
          <Button
            type="button"
            className="bg-blue-600 text-white hover:bg-blue-500"
            onClick={onConfirm}
            disabled={files.length === 0 || isDownloading}
          >
            {isDownloading ? "Preparing..." : "Download as ZIP"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
