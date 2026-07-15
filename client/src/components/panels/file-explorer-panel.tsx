/* Task workspace file browser with local task/file selection and bulk download controls. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MultiFileDownloadDialog } from "@/components/dialogs/multi-file-download-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { apiFetch } from "@/lib/api-config";
import { triggerBrowserDownload } from "@/lib/browser-download";
import { apiRequest } from "@/lib/queryClient";
import { cn } from "@/lib/utils";
import type { Task } from "@/types";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Download,
  Folder,
  FolderOpen,
  RefreshCw,
  Search,
  X,
} from "lucide-react";

type TreeNodeType = "file" | "folder";

interface FileTreeNode {
  name: string;
  type: TreeNodeType;
  path: string;
  size: number | null;
  modified: string;
  content_availability?: string | null;
  children: FileTreeNode[];
}

interface SearchResultNode {
  name: string;
  type: "file";
  path: string;
  size: number;
  modified: string;
  content_availability?: string | null;
}

interface SearchResponse {
  query: string;
  results: SearchResultNode[];
  total_count: number;
  truncated: boolean;
}

export interface FileExplorerSelection {
  taskId: number | null;
  filePath: string | null;
}

interface FileExplorerPanelProps {
  selectedTaskId?: number | null;
  selectedFile?: string | null;
  onSelectionChange?: (selection: FileExplorerSelection) => void;
}

interface DownloadSelectionItem {
  path: string;
  name: string;
  size: number | null;
  contentAvailability: string;
}

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function parseTaskId(taskValue: string | null): number | null {
  const parsed = taskValue ? Number(taskValue) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
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

async function fetchTasks(): Promise<Task[]> {
  return requestJson<Task[]>("/api/tasks/");
}

async function fetchTree(taskId: number): Promise<FileTreeNode> {
  return requestJson<FileTreeNode>(`/api/tasks/${taskId}/files/tree`);
}

async function fetchSearch(taskId: number, query: string): Promise<SearchResponse> {
  const encoded = encodeURIComponent(query);
  return requestJson<SearchResponse>(`/api/tasks/${taskId}/files/search?q=${encoded}`);
}

function sortTasks(tasks: Task[]): Task[] {
  const running = tasks.filter((task) => task.status === "running");
  const others = tasks.filter((task) => task.status !== "running");
  others.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  running.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  return [...running, ...others];
}

function formatFileSize(size: number | null): string {
  if (size == null) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function normalizeContentAvailability(value: unknown): string {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (!normalized) {
    return "available_inline";
  }
  return normalized;
}

function isUploadPendingAvailability(value: unknown): boolean {
  return normalizeContentAvailability(value) === "upload_pending";
}

function isUploadFailedAvailability(value: unknown): boolean {
  return normalizeContentAvailability(value) === "upload_failed";
}

function isFileInteractionBlocked(value: unknown): boolean {
  return isUploadPendingAvailability(value) || isUploadFailedAvailability(value);
}

function parseFilenameFromPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

function parseDownloadFilename(contentDisposition: string | null): string | null {
  if (!contentDisposition) {
    return null;
  }

  const utf8Match = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    return decodeURIComponent(utf8Match[1].replace(/["']/g, ""));
  }

  const simpleMatch = contentDisposition.match(/filename="?([^"]+)"?/i);
  return simpleMatch?.[1] ?? null;
}

function buildTreeFromSearchResults(results: SearchResultNode[]): FileTreeNode {
  const root: FileTreeNode = {
    name: "workspace",
    type: "folder",
    path: "/",
    size: null,
    modified: "",
    children: [],
  };

  const folderMap = new Map<string, FileTreeNode>([["/", root]]);

  for (const result of results) {
    const parts = result.path.split("/").filter(Boolean);
    let currentPath = "";

    for (let index = 0; index < parts.length - 1; index += 1) {
      const segment = parts[index];
      currentPath = `${currentPath}/${segment}`;
      if (!folderMap.has(currentPath)) {
        const folderNode: FileTreeNode = {
          name: segment,
          type: "folder",
          path: currentPath,
          size: null,
          modified: result.modified,
          children: [],
        };

        const parentPath = currentPath.split("/").slice(0, -1).join("/") || "/";
        const parent = folderMap.get(parentPath);
        if (parent) {
          parent.children.push(folderNode);
        }
        folderMap.set(currentPath, folderNode);
      }
    }

    const fileNode: FileTreeNode = {
      ...result,
      children: [],
    };
    const parentPath = result.path.split("/").slice(0, -1).join("/") || "/";
    const parent = folderMap.get(parentPath);
    if (parent) {
      parent.children.push(fileNode);
    }
  }

  const sortTree = (node: FileTreeNode): void => {
    node.children.sort((a, b) => {
      if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    node.children.forEach(sortTree);
  };
  sortTree(root);
  return root;
}

function collectFolderPaths(node: FileTreeNode): Set<string> {
  const paths = new Set<string>();

  const walk = (current: FileTreeNode): void => {
    if (current.type === "folder") {
      paths.add(current.path);
      current.children.forEach(walk);
    }
  };

  walk(node);
  return paths;
}

function collectFilePaths(node: FileTreeNode): Set<string> {
  const paths = new Set<string>();

  const walk = (current: FileTreeNode): void => {
    if (current.type === "file") {
      paths.add(current.path);
      return;
    }
    current.children.forEach(walk);
  };

  walk(node);
  return paths;
}

function collectSelectableFilePaths(node: FileTreeNode): Set<string> {
  const paths = new Set<string>();

  const walk = (current: FileTreeNode): void => {
    if (current.type === "file") {
      if (!isFileInteractionBlocked(current.content_availability)) {
        paths.add(current.path);
      }
      return;
    }
    current.children.forEach(walk);
  };

  walk(node);
  return paths;
}

function buildFileMetadataMap(node: FileTreeNode): Map<string, DownloadSelectionItem> {
  const metadata = new Map<string, DownloadSelectionItem>();

  const walk = (current: FileTreeNode): void => {
    if (current.type === "file") {
      metadata.set(current.path, {
        path: current.path,
        name: current.name,
        size: current.size,
        contentAvailability: normalizeContentAvailability(current.content_availability),
      });
      return;
    }
    current.children.forEach(walk);
  };

  walk(node);
  return metadata;
}

function expandPathSegments(path: string): Set<string> {
  const expanded = new Set<string>();
  const parts = path.split("/").filter(Boolean);
  let current = "";
  expanded.add("/");
  for (let index = 0; index < parts.length - 1; index += 1) {
    current = `${current}/${parts[index]}`;
    expanded.add(current);
  }
  return expanded;
}

function getCountSummary(root: FileTreeNode | null): { files: number; folders: number } {
  if (!root) return { files: 0, folders: 0 };

  let files = 0;
  let folders = 0;

  const walk = (node: FileTreeNode): void => {
    if (node.type === "folder") {
      if (node.path !== "/") {
        folders += 1;
      }
      node.children.forEach(walk);
      return;
    }
    files += 1;
  };

  walk(root);
  return { files, folders };
}

function getPathSegments(path: string | null): Array<{ label: string; path: string }> {
  const segments: Array<{ label: string; path: string }> = [{ label: "workspace", path: "/" }];
  if (!path) return segments;

  const parts = path.split("/").filter(Boolean);
  let currentPath = "";

  for (let index = 0; index < parts.length; index += 1) {
    currentPath = `${currentPath}/${parts[index]}`;
    segments.push({ label: parts[index], path: currentPath });
  }
  return segments;
}

export function FileExplorerPanel({
  selectedTaskId: controlledSelectedTaskId,
  selectedFile: controlledSelectedFile,
  onSelectionChange,
}: FileExplorerPanelProps = {}) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [internalSelectedTaskId, setInternalSelectedTaskId] = useState<number | null>(null);
  const [internalSelectedFile, setInternalSelectedFile] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set(["/"]));
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());
  const [showDownloadDialog, setShowDownloadDialog] = useState(false);

  const treeItemRefs = useRef<Map<string, HTMLButtonElement | null>>(new Map());

  const selectedTaskId =
    controlledSelectedTaskId === undefined ? internalSelectedTaskId : controlledSelectedTaskId;
  const selectedFile =
    controlledSelectedFile === undefined ? internalSelectedFile : controlledSelectedFile;

  const setSelection = useCallback(
    (next: FileExplorerSelection) => {
      if (controlledSelectedTaskId === undefined) {
        setInternalSelectedTaskId(next.taskId);
      }
      if (controlledSelectedFile === undefined) {
        setInternalSelectedFile(next.filePath);
      }
      onSelectionChange?.(next);
    },
    [controlledSelectedFile, controlledSelectedTaskId, onSelectionChange],
  );

  const tasksQuery = useQuery<Task[]>({
    queryKey: ["tasks", "selector"],
    queryFn: fetchTasks,
  });
  const tasks = tasksQuery.data ?? [];

  const sortedTasks = useMemo(() => sortTasks(tasks), [tasks]);

  const treeQuery = useQuery<FileTreeNode>({
    queryKey: ["files", selectedTaskId, "tree"],
    queryFn: () => fetchTree(selectedTaskId as number),
    enabled: selectedTaskId != null,
  });

  const trimmedSearch = searchQuery.trim();
  const searchQueryResult = useQuery<SearchResponse>({
    queryKey: ["files", selectedTaskId, "search", trimmedSearch],
    queryFn: () => fetchSearch(selectedTaskId as number, trimmedSearch),
    enabled: selectedTaskId != null && trimmedSearch.length > 0,
  });

  const activeTree = useMemo(() => {
    if (trimmedSearch.length > 0) {
      return searchQueryResult.data ? buildTreeFromSearchResults(searchQueryResult.data.results) : null;
    }
    return treeQuery.data ?? null;
  }, [trimmedSearch, searchQueryResult.data, treeQuery.data]);

  useEffect(() => {
    if (!treeQuery.data || trimmedSearch.length > 0) {
      return;
    }

    const availableFolders = collectFolderPaths(treeQuery.data);
    const nextExpanded = new Set<string>();
    expandedFolders.forEach((path) => {
      if (availableFolders.has(path)) {
        nextExpanded.add(path);
      }
    });
    if (!nextExpanded.has("/")) {
      nextExpanded.add("/");
    }

    if (nextExpanded.size !== expandedFolders.size) {
      setExpandedFolders(nextExpanded);
    }

    const files = collectFilePaths(treeQuery.data);
    const selectableFiles = collectSelectableFilePaths(treeQuery.data);
    if (selectedFile && (!files.has(selectedFile) || !selectableFiles.has(selectedFile))) {
      setSelection({ taskId: selectedTaskId, filePath: null });
    }

    setSelectedFiles((prev) => {
      const next = new Set<string>();
      prev.forEach((path) => {
        if (files.has(path) && selectableFiles.has(path)) {
          next.add(path);
        }
      });
      return next.size === prev.size ? prev : next;
    });
  }, [treeQuery.data, expandedFolders, selectedFile, selectedTaskId, setSelection, trimmedSearch]);

  useEffect(() => {
    if (!selectedTaskId) {
      return;
    }

    if (!tasksQuery.isSuccess) {
      return;
    }

    const exists = sortedTasks.some((task) => task.id === selectedTaskId);
    if (!exists) {
      setSelection({ taskId: null, filePath: null });
      setSelectedFiles(new Set());
      setShowDownloadDialog(false);
      setSearchQuery("");
      setExpandedFolders(new Set(["/"]));
    }
  }, [selectedTaskId, setSelection, sortedTasks, tasksQuery.isSuccess]);

  const handleTaskChange = useCallback((rawValue: string) => {
    const value = parseTaskId(rawValue);
    if (value == null) {
      return;
    }

    setSelection({ taskId: value, filePath: null });
    setSelectedFiles(new Set());
    setShowDownloadDialog(false);
    setSearchQuery("");
    setExpandedFolders(new Set(["/"]));
  }, [setSelection]);

  const toggleFolder = useCallback((path: string) => {
    setExpandedFolders((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      next.add("/");
      return next;
    });
  }, []);

  const handleFileSelection = useCallback((path: string) => {
    setSelection({ taskId: selectedTaskId ?? null, filePath: path });

    if (trimmedSearch.length > 0) {
      setSearchQuery("");
    }

    setExpandedFolders((prev) => {
      const next = new Set(prev);
      expandPathSegments(path).forEach((segment) => next.add(segment));
      return next;
    });
  }, [selectedTaskId, setSelection, trimmedSearch.length]);

  const handleFileToggleSelection = useCallback((path: string) => {
    setSelectedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }, []);

  const handleBreadcrumbClick = useCallback((path: string) => {
    setSelection({ taskId: selectedTaskId, filePath: null });

    if (path !== "/") {
      setExpandedFolders((prev) => {
        const next = new Set(prev);
        expandPathSegments(`${path}/_placeholder`).forEach((segment) => next.add(segment));
        next.add(path);
        return next;
      });

      requestAnimationFrame(() => {
        const target = treeItemRefs.current.get(path);
        target?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    }
  }, [selectedTaskId, setSelection]);

  const handleRefresh = useCallback(async () => {
    if (!selectedTaskId) {
      return;
    }

    await treeQuery.refetch();
    if (selectedTaskId && selectedFile) {
      await queryClient.invalidateQueries({
        queryKey: ["files", selectedTaskId, "content", selectedFile],
      });
    }
    toast({
      title: "Files refreshed",
    });
  }, [queryClient, selectedFile, selectedTaskId, toast, treeQuery]);

  const selectedDownloadItems = useMemo(() => {
    if (!treeQuery.data) {
      return [] as DownloadSelectionItem[];
    }

    const metadataMap = buildFileMetadataMap(treeQuery.data);
    return Array.from(selectedFiles)
      .map(
        (path) =>
          metadataMap.get(path) ?? {
            path,
            name: parseFilenameFromPath(path),
            size: null,
            contentAvailability: "available_inline",
          },
      )
      .filter((item) => !isFileInteractionBlocked(item.contentAvailability))
      .sort((a, b) => a.path.localeCompare(b.path));
  }, [selectedFiles, treeQuery.data]);

  const downloadMultipleMutation = useMutation({
    mutationFn: async (payload: { taskId: number; paths: string[] }) => {
      const response = await apiRequest(
        "POST",
        `/api/tasks/${payload.taskId}/files/download-multiple`,
        { paths: payload.paths },
      );

      if (!response.ok) {
        let detail = `Download failed (${response.status})`;
        try {
          const json = await response.json();
          detail = json?.detail ?? json?.message ?? detail;
        } catch {
          const text = await response.text().catch(() => "");
          if (text) {
            detail = text;
          }
        }
        throw new Error(detail);
      }

      const blob = await response.blob();
      const filename =
        parseDownloadFilename(response.headers.get("content-disposition")) ?? "workspace-files.zip";
      return { blob, filename };
    },
  });

  const handleMultiDownloadConfirm = useCallback(async () => {
    if (!selectedTaskId || selectedDownloadItems.length === 0) {
      return;
    }

    try {
      const paths = selectedDownloadItems.map((item) => item.path);
      const { blob, filename } = await downloadMultipleMutation.mutateAsync({
        taskId: selectedTaskId,
        paths,
      });

      triggerBrowserDownload(blob, filename);
      toast({
        title: `Downloading ${filename}`,
        description: `${paths.length} file${paths.length === 1 ? "" : "s"} selected`,
      });
      setShowDownloadDialog(false);
      setSelectedFiles(new Set());
    } catch (error) {
      toast({
        title: "Download failed",
        description: error instanceof Error ? error.message : "Please try again.",
        variant: "destructive",
      });
    }
  }, [downloadMultipleMutation, selectedDownloadItems, selectedTaskId, toast]);

  const activeExpandedFolders = useMemo(() => {
    if (!activeTree) {
      return new Set<string>();
    }

    if (trimmedSearch.length === 0) {
      return expandedFolders;
    }

    return collectFolderPaths(activeTree);
  }, [activeTree, expandedFolders, trimmedSearch.length]);

  const isLoading = treeQuery.isLoading || (trimmedSearch.length > 0 && searchQueryResult.isLoading);
  const hasSearchNoResults = trimmedSearch.length > 0 && !isLoading && (searchQueryResult.data?.results.length ?? 0) === 0;

  const treeCounts = useMemo(() => getCountSummary(treeQuery.data ?? null), [treeQuery.data]);
  const breadcrumbSegments = useMemo(() => getPathSegments(selectedFile), [selectedFile]);

  const renderTree = useCallback(
    (nodes: FileTreeNode[], depth = 0): JSX.Element[] => {
      return nodes.map((node) => {
        const isFolder = node.type === "folder";
        const isExpanded = isFolder && activeExpandedFolders.has(node.path);
        const isSingleSelected = node.type === "file" && node.path === selectedFile;
        const isMultiSelected = node.type === "file" && selectedFiles.has(node.path);
        const contentAvailability = normalizeContentAvailability(node.content_availability);
        const uploadPending = isUploadPendingAvailability(contentAvailability);
        const uploadFailed = isUploadFailedAvailability(contentAvailability);
        const blockedFileInteraction = node.type === "file" && isFileInteractionBlocked(contentAvailability);

        return (
          <div key={node.path}>
            <button
              type="button"
              ref={(value) => {
                treeItemRefs.current.set(node.path, value);
              }}
              className={cn(
                "group flex w-full items-center gap-2 rounded px-2 py-1 text-left text-sm hover:bg-slate-800",
                isSingleSelected || isMultiSelected ? "bg-slate-700 text-white" : "text-slate-200",
              )}
              style={{ paddingLeft: `${depth * 14 + 8}px` }}
              onClick={(event) => {
                if (isFolder) {
                  toggleFolder(node.path);
                  return;
                }
                if (blockedFileInteraction) {
                  toast({
                    title: uploadPending ? "Upload in progress" : "Upload failed",
                    description: uploadPending
                      ? "This file is still uploading. Preview and download are unavailable until the upload completes."
                      : "This file upload failed. Preview and download are unavailable.",
                    variant: "destructive",
                  });
                  return;
                }
                if (event.ctrlKey || event.metaKey) {
                  handleFileToggleSelection(node.path);
                  return;
                }
                handleFileSelection(node.path);
              }}
            >
              {isFolder ? (
                <>
                  {isExpanded ? (
                    <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5 text-slate-400" />
                  )}
                  {isExpanded ? (
                    <FolderOpen className="h-4 w-4 text-blue-400" />
                  ) : (
                    <Folder className="h-4 w-4 text-blue-400" />
                  )}
                </>
              ) : (
                <>
                  <span className="inline-block w-3.5" />
                  <span className="inline-flex h-4 w-4 items-center justify-center rounded border border-slate-600 text-[10px] text-slate-300">
                    F
                  </span>
                </>
              )}

              <span className="truncate">{node.name}</span>
              {node.type === "file" && uploadPending ? (
                <Badge className="ml-auto bg-amber-900/40 text-[10px] text-amber-200" variant="secondary">
                  Uploading
                </Badge>
              ) : null}
              {node.type === "file" && uploadFailed ? (
                <Badge className="ml-auto bg-rose-900/40 text-[10px] text-rose-200" variant="secondary">
                  Upload failed
                </Badge>
              ) : null}
              {node.type === "file" && node.size != null ? (
                <Badge
                  className={cn(
                    (uploadPending || uploadFailed) ? "ml-2" : "ml-auto",
                    "bg-slate-800 text-[10px] text-slate-300",
                  )}
                  variant="secondary"
                >
                  {formatFileSize(node.size)}
                </Badge>
              ) : null}
              {node.type === "file" && selectedFiles.has(node.path) ? (
                <Check className="h-3.5 w-3.5 text-emerald-300" aria-label="Selected for download" />
              ) : null}
            </button>

            {isFolder && isExpanded && node.children.length > 0 ? renderTree(node.children, depth + 1) : null}
          </div>
        );
      });
    },
    [
      activeExpandedFolders,
      handleFileSelection,
      handleFileToggleSelection,
      selectedFile,
      selectedFiles,
      toast,
      toggleFolder,
    ],
  );

  const noTaskSelected = selectedTaskId == null;
  const workspaceNotReady =
    treeQuery.isError &&
    treeQuery.error instanceof ApiError &&
    treeQuery.error.status === 404 &&
    trimmedSearch.length === 0;
  const emptyWorkspace =
    !noTaskSelected &&
    !workspaceNotReady &&
    !treeQuery.isLoading &&
    !treeQuery.isError &&
    (treeQuery.data?.children.length ?? 0) === 0;

  return (
    <>
      <div className="flex h-full min-h-0 flex-col border-r border-slate-700 bg-slate-900">
        <div className="flex items-center justify-between border-b border-slate-700 bg-slate-800 px-3 py-2">
          <div className="flex items-center gap-2">
            <Folder className="h-4 w-4 text-blue-400" />
            <span className="text-sm font-semibold text-white">File Explorer</span>
          </div>

          <div className="flex items-center gap-2">
            {selectedFiles.size > 0 ? (
              <Button
                type="button"
                size="sm"
                className="h-7 bg-blue-600 px-2 text-xs text-white hover:bg-blue-500"
                onClick={() => setShowDownloadDialog(true)}
                aria-label="Download selected files"
              >
                <Download className="h-3.5 w-3.5" />
                Download Selected ({selectedFiles.size})
              </Button>
            ) : null}

            <Select
              value={selectedTaskId != null ? String(selectedTaskId) : undefined}
              onValueChange={handleTaskChange}
            >
              <SelectTrigger aria-label="Select task" className="h-7 w-[140px] px-2 text-xs">
                <SelectValue placeholder="Select a task..." />
              </SelectTrigger>
              <SelectContent>
                {sortedTasks.map((task) => (
                  <SelectItem key={task.id} value={String(task.id)} className="text-xs">
                    {task.name} (#{task.id})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0 text-slate-300 hover:text-white"
              onClick={handleRefresh}
              disabled={selectedTaskId == null || treeQuery.isFetching}
              aria-label="Refresh files"
            >
              <RefreshCw className={cn("h-4 w-4", treeQuery.isFetching && "animate-spin")} />
            </Button>
          </div>
        </div>

        <div className="border-b border-slate-700 px-3 py-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <Input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search files..."
              className="h-8 border-slate-600 bg-slate-800 pl-8 pr-8 text-sm text-white"
              disabled={selectedTaskId == null}
            />
            {trimmedSearch.length > 0 ? (
              <button
                type="button"
                aria-label="Clear search"
                className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-200"
                onClick={() => setSearchQuery("")}
              >
                <X className="h-4 w-4" />
              </button>
            ) : null}
          </div>
        </div>

        <div data-testid="file-breadcrumb" className="border-b border-slate-700 px-3 py-2 text-xs text-slate-300">
          {breadcrumbSegments.map((segment, index) => {
            const isLast = index === breadcrumbSegments.length - 1;
            if (isLast) {
              return (
                <span key={segment.path} className="font-medium text-slate-100">
                  {index === 0 ? `/workspace` : segment.label}
                </span>
              );
            }

            return (
              <span key={segment.path}>
                <button
                  type="button"
                  className="hover:text-slate-100"
                  onClick={() => handleBreadcrumbClick(segment.path)}
                >
                  {index === 0 ? `/workspace` : segment.label}
                </button>
                <span className="px-1 text-slate-500">/</span>
              </span>
            );
          })}
        </div>

        <ScrollArea className="flex-1 min-h-0">
          <div className="p-2">
            {noTaskSelected ? (
              <div className="px-3 py-8 text-center text-sm text-slate-400">
                Select a task from the dropdown to view files
              </div>
            ) : null}

            {workspaceNotReady ? (
              <div className="px-3 py-8 text-center text-sm text-slate-400">
                Workspace is being prepared. Please wait...
              </div>
            ) : null}

            {emptyWorkspace ? (
              <div className="px-3 py-8 text-center text-sm text-slate-400">
                No files yet. The AI agent will create files as it works.
              </div>
            ) : null}

            {hasSearchNoResults ? (
              <div className="px-3 py-8 text-center text-sm text-slate-400">
                No files match "{trimmedSearch}"
              </div>
            ) : null}

            {isLoading ? (
              <div className="px-3 py-8 text-center text-sm text-slate-400">Loading files...</div>
            ) : null}

            {!noTaskSelected &&
            !workspaceNotReady &&
            !emptyWorkspace &&
            !hasSearchNoResults &&
            !isLoading &&
            activeTree ? (
              <div>{renderTree(activeTree.children)}</div>
            ) : null}
          </div>
        </ScrollArea>

        <div className="border-t border-slate-700 bg-slate-800 px-3 py-1.5 text-xs text-slate-400">
          {treeCounts.folders} folders, {treeCounts.files} files
        </div>
      </div>

      <MultiFileDownloadDialog
        open={showDownloadDialog}
        files={selectedDownloadItems}
        isDownloading={downloadMultipleMutation.isPending}
        onCancel={() => setShowDownloadDialog(false)}
        onConfirm={handleMultiDownloadConfirm}
      />
    </>
  );
}
