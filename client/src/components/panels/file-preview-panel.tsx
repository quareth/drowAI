/* Task workspace file preview panel driven by explicit task and file selection props. */

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Copy, Download, FileText, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useToast } from "@/hooks/use-toast";
import { apiFetch } from "@/lib/api-config";
import { cn } from "@/lib/utils";
import { JsonViewer } from "@/components/ui/json-viewer";

type PreviewType = "markdown" | "json" | "xml" | "text" | "binary";

interface FilePreviewResponse {
  path: string;
  name: string;
  size: number;
  type: string;
  content: string;
  encoding: string;
  preview_type: PreviewType;
  is_truncated: boolean;
  modified: string;
  metadata: {
    is_valid_json: boolean | null;
    is_valid_xml: boolean | null;
    line_count: number | null;
  };
}

interface FilePreviewPanelProps {
  taskId?: number | null;
  filePath?: string | null;
  onDownloadRequested?: (file: FilePreviewResponse) => void;
}

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function requestJson<T>(endpoint: string): Promise<T> {
  const response = await apiFetch(endpoint, { method: "GET" });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      detail = payload?.detail ?? payload?.message ?? detail;
    } catch {
      const text = await response.text().catch(() => "");
      if (text) {
        detail = text;
      }
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

async function fetchFileContent(taskId: number, filePath: string): Promise<FilePreviewResponse> {
  const encoded = encodeURIComponent(filePath);
  return requestJson<FilePreviewResponse>(`/api/tasks/${taskId}/files/content?path=${encoded}`);
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function decodeHtmlEntities(input: string): string {
  if (!input) return "";
  if (typeof document === "undefined") return input;
  const el = document.createElement("textarea");
  el.innerHTML = input;
  return el.value;
}

interface XmlSyntaxViewProps {
  content: string;
}

function XmlSyntaxView({ content }: XmlSyntaxViewProps) {
  const lines = content.split(/\r?\n/);

  const renderTag = (segment: string, key: string): JSX.Element => {
    const match = segment.match(/^<(\/?)([A-Za-z_][A-Za-z0-9_.:-]*)([\s\S]*?)(\/?)>$/);
    if (!match) {
      return <span key={key} className="text-slate-300">{segment}</span>;
    }

    const [, slash, tagName, attrsRaw, selfClosing] = match;
    const attrs = attrsRaw.trim();
    const attrParts: JSX.Element[] = [];
    const attrRegex = /([A-Za-z_][A-Za-z0-9_.:-]*)(\s*=\s*("[^"]*"|'[^']*'|[^\s"'=<>`]+))?/g;
    let attrMatch: RegExpExecArray | null;
    let attrIndex = 0;

    while ((attrMatch = attrRegex.exec(attrs)) !== null) {
      const attrName = attrMatch[1];
      const attrValue = attrMatch[3];
      attrParts.push(
        <span key={`${key}-space-${attrIndex}`} className="text-slate-300">
          {" "}
        </span>,
      );
      attrParts.push(
        <span key={`${key}-name-${attrIndex}`} className="text-amber-300">
          {attrName}
        </span>,
      );
      if (attrValue) {
        const normalized = attrValue.trim();
        attrParts.push(
          <span key={`${key}-eq-${attrIndex}`} className="text-slate-300">
            =
          </span>,
        );
        attrParts.push(
          <span key={`${key}-value-${attrIndex}`} className="text-emerald-300">
            {normalized}
          </span>,
        );
      }
      attrIndex += 1;
    }

    return (
      <span key={key}>
        <span className="text-cyan-300">&lt;</span>
        {slash ? <span className="text-cyan-300">/</span> : null}
        <span className="text-blue-300">{tagName}</span>
        {attrParts}
        {selfClosing ? <span className="text-cyan-300">/</span> : null}
        <span className="text-cyan-300">&gt;</span>
      </span>
    );
  };

  return (
    <pre className="font-mono text-xs leading-6 text-slate-200">
      {lines.map((line, lineIndex) => {
        const segments = line.match(/<[^>]+>|[^<]+/g) ?? [line];
        return (
          <div key={`line-${lineIndex}`}>
            {segments.map((segment, segmentIndex) => {
              const segmentKey = `line-${lineIndex}-segment-${segmentIndex}`;
              if (segment.startsWith("<") && segment.endsWith(">")) {
                return renderTag(segment, segmentKey);
              }
              return (
                <span key={segmentKey} className="text-slate-300">
                  {segment}
                </span>
              );
            })}
          </div>
        );
      })}
    </pre>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex h-full items-center justify-center bg-slate-950">
      <div className="text-center text-slate-400">
        <FileText className="mx-auto mb-4 h-12 w-12 opacity-50" />
        <p>{message}</p>
      </div>
    </div>
  );
}

export function FilePreviewPanel({
  taskId = null,
  filePath = null,
  onDownloadRequested,
}: FilePreviewPanelProps) {
  const { toast } = useToast();
  const lastErrorKeyRef = useRef<string | null>(null);
  const selectedTaskId = taskId != null && taskId > 0 ? taskId : null;
  const selectedFilePath = filePath;

  const fileQuery = useQuery<FilePreviewResponse>({
    queryKey: ["files", selectedTaskId, "content", selectedFilePath],
    queryFn: () => fetchFileContent(selectedTaskId as number, selectedFilePath as string),
    enabled: selectedTaskId != null && Boolean(selectedFilePath),
  });

  useEffect(() => {
    if (!fileQuery.error) {
      lastErrorKeyRef.current = null;
      return;
    }
    const error = fileQuery.error as Error;
    const nextErrorKey = `${selectedTaskId}:${selectedFilePath}:${error.message}`;
    if (lastErrorKeyRef.current === nextErrorKey) {
      return;
    }
    lastErrorKeyRef.current = nextErrorKey;
    toast({
      title: "Failed to load file preview",
      description: error.message,
      variant: "destructive",
    });
  }, [fileQuery.error, selectedFilePath, selectedTaskId, toast]);

  const activeFile = fileQuery.data ?? null;
  const decodedContent = useMemo(
    () => decodeHtmlEntities(activeFile?.content ?? ""),
    [activeFile?.content],
  );

  const parsedJson = useMemo(() => {
    if (!activeFile || activeFile.preview_type !== "json") {
      return null;
    }
    try {
      return JSON.parse(activeFile.content);
    } catch {
      return null;
    }
  }, [activeFile]);

  const handleCopyPath = useCallback(async () => {
    if (!activeFile) {
      return;
    }

    try {
      await navigator.clipboard.writeText(activeFile.path);
      toast({
        title: "Path copied",
        description: activeFile.path,
      });
    } catch {
      toast({
        title: "Copy failed",
        description: "Could not copy path to clipboard.",
        variant: "destructive",
      });
    }
  }, [activeFile, toast]);

  const handleDownload = useCallback(async () => {
    if (!activeFile) {
      return;
    }
    if (onDownloadRequested) {
      onDownloadRequested(activeFile);
      return;
    }
    if (!selectedTaskId) {
      return;
    }

    toast({
      title: `Downloading ${activeFile.name}`,
    });

    try {
      const encodedPath = encodeURIComponent(activeFile.path);
      const response = await apiFetch(
        `/api/tasks/${selectedTaskId}/files/download?path=${encodedPath}`,
        { method: "GET" },
      );

      if (!response.ok) {
        let detail = `Download failed (${response.status})`;
        try {
          const payload = await response.json();
          detail = payload?.detail ?? payload?.message ?? detail;
        } catch {
          const text = await response.text().catch(() => "");
          if (text) {
            detail = text;
          }
        }
        throw new Error(detail);
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = activeFile.name;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      toast({
        title: "Download failed",
        description: error instanceof Error ? error.message : "Please try again.",
        variant: "destructive",
      });
    }
  }, [activeFile, onDownloadRequested, selectedTaskId, toast]);

  if (selectedTaskId == null || !selectedFilePath) {
    return <EmptyState message="Select a file to preview" />;
  }

  if (fileQuery.isLoading) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-950 text-slate-400">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading preview...
      </div>
    );
  }

  if (fileQuery.isError || !activeFile) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-950 text-slate-400">
        <div className="text-center">
          <AlertCircle className="mx-auto mb-3 h-6 w-6 text-red-400" />
          <p>Could not load this file preview.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-slate-950">
      <div className="flex items-center justify-between border-b border-slate-700 bg-slate-900 px-3 py-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-slate-100">{activeFile.name}</p>
          <p className="text-xs text-slate-400">
            {activeFile.path} • {formatFileSize(activeFile.size)}
          </p>
        </div>
        <div className="ml-3 flex items-center gap-1.5">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 px-2 text-slate-300 hover:text-white"
            onClick={handleCopyPath}
            aria-label="Copy file path"
            title="Copy file path"
          >
            <Copy className="h-4 w-4" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 px-2 text-slate-300 hover:text-white"
            onClick={handleDownload}
            aria-label="Download file"
            title="Download file"
          >
            <Download className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <ScrollArea className="flex-1 min-h-0">
        <div className="p-4">
          {activeFile.preview_type === "markdown" ? (
            <article
              className="prose prose-sm prose-invert max-w-none prose-headings:text-slate-100 prose-p:text-slate-200 prose-strong:text-slate-100 prose-code:text-emerald-300 prose-pre:bg-slate-900 prose-pre:text-slate-200"
              dangerouslySetInnerHTML={{ __html: activeFile.content }}
            />
          ) : null}

          {activeFile.preview_type === "json" ? (
            parsedJson ? (
              <JsonViewer data={parsedJson} initialExpanded />
            ) : (
              <div className="space-y-3">
                <p className="text-sm text-amber-300">Invalid JSON content. Showing raw text.</p>
                <pre className="overflow-x-auto rounded border border-slate-700 bg-slate-900 p-3 font-mono text-xs text-slate-200">
                  {decodedContent}
                </pre>
              </div>
            )
          ) : null}

          {activeFile.preview_type === "xml" ? (
            activeFile.metadata.is_valid_xml ? (
              <div className="overflow-x-auto rounded border border-slate-700 bg-slate-900 p-3">
                <XmlSyntaxView content={decodedContent} />
              </div>
            ) : (
              <div className="space-y-3">
                <p className="text-sm text-amber-300">Invalid XML content. Showing raw text.</p>
                <pre className="overflow-x-auto rounded border border-slate-700 bg-slate-900 p-3 font-mono text-xs text-slate-200">
                  {decodedContent}
                </pre>
              </div>
            )
          ) : null}

          {activeFile.preview_type === "text" ? (
            <pre
              className={cn(
                "overflow-x-auto rounded border border-slate-700 bg-slate-900 p-3",
                "font-mono text-xs leading-6 text-slate-200 whitespace-pre-wrap break-words",
              )}
            >
              {decodedContent}
            </pre>
          ) : null}

          {activeFile.preview_type === "binary" ? (
            <div className="rounded border border-slate-700 bg-slate-900 p-6 text-center">
              <FileText className="mx-auto mb-3 h-8 w-8 text-slate-400" />
              <p className="text-sm text-slate-200">Cannot preview this file type</p>
              <p className="mt-1 text-xs text-slate-400">
                This file appears to be binary or unsupported for inline preview.
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="mt-4 border-slate-600 bg-slate-800 text-slate-100 hover:bg-slate-700"
                onClick={handleDownload}
              >
                <Download className="mr-1.5 h-4 w-4" />
                Download file
              </Button>
            </div>
          ) : null}
        </div>
      </ScrollArea>
    </div>
  );
}
