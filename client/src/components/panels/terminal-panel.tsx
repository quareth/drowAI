/**
 * Global task terminal dock for opening and managing per-task shell sessions.
 */
import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from "@/components/ui/resizable";
// We manage per-terminal WebSocket connections locally for persistence
// import { useTerminal } from "@/hooks/use-terminal"; // not used
import { useToast } from "@/hooks/use-toast";
import { useQuery } from "@tanstack/react-query";
import {
  Terminal,
  Copy,
  Trash2,
  ChevronUp,
  Square,
  Plus,
  Minus,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { apiFetch } from "@/lib/api-config";
import { queryClient } from "@/lib/queryClient";
import { useTenantContext } from "@/hooks/use-tenant-context";
import { TENANT_ACTIONS, hasTenantAction, toTenantActionSet } from "@/lib/tenant-permissions";
// Add xterm imports
import { Terminal as XTerm } from "xterm";
import { FitAddon } from "xterm-addon-fit";
import "xterm/css/xterm.css";
// import { wsConfig } from "@/utils/websocket-config";
import { useTerminalSockets } from "@/hooks/useTerminalSockets";
import { useWorkbenchStateSnapshot } from "@/state/workbench-state-store";
import {
  clearTaskTerminalStorage,
  getTerminalBuffer,
  readTerminalList,
  setTerminalBuffer,
  setTerminalSessionId,
  writeTerminalList,
  type StoredTerminalRecord,
} from "@/lib/terminal-storage";

// WebSocket creation handled by useTerminalSockets hook

interface TerminalPanelProps {
  isCollapsed?: boolean;
  onToggleCollapse?: () => void;
}

interface Task {
  id: number;
  name: string;
  status: string;
}

interface TerminalInstance {
  id: string;
  taskId: number | null;
  taskName: string;
  isActive: boolean;
  isConnected: boolean;
  sessionId: string | null;
}

function toTerminalInstances(saved: StoredTerminalRecord[]): TerminalInstance[] {
  return saved.map((item) => ({
    id: item.id || `terminal-${item.taskId ?? Date.now()}`,
    taskId: item.taskId ?? null,
    taskName: item.taskName || (item.taskId != null ? `Task ${item.taskId}` : "Terminal"),
    isActive: Boolean(item.isActive),
    isConnected: false,
    sessionId: null,
  }));
}

function toStoredTerminals(terminals: TerminalInstance[]): StoredTerminalRecord[] {
  return terminals.map((terminal) => ({
    id: terminal.id,
    taskId: terminal.taskId,
    taskName: terminal.taskName,
    isActive: terminal.isActive,
  }));
}

export function TerminalPanel({ isCollapsed = false, onToggleCollapse }: TerminalPanelProps) {
  const { terminalTaskId, terminalRequestNonce } = useWorkbenchStateSnapshot();
  const { effectivePermissions } = useTenantContext();
  const canControlTask = hasTenantAction(
    toTenantActionSet(effectivePermissions),
    TENANT_ACTIONS.taskControl,
  );
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const [output, setOutput] = useState<string[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [terminalInstances, setTerminalInstances] = useState<TerminalInstance[]>([]);
  const [activeTerminalId, setActiveTerminalId] = useState<string | null>(null);
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const xtermContainerRef = useRef<HTMLDivElement>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const lastResizeSentRef = useRef<string | null>(null);
  const pendingResizeRef = useRef<{ cols: number; rows: number } | null>(null);
  const resizeTimerRef = useRef<number | null>(null);
  
  // Track current active terminal id for stable access inside WS handlers
  const activeTerminalIdRef = useRef<string | null>(null);
  useEffect(() => { activeTerminalIdRef.current = activeTerminalId; }, [activeTerminalId]);
  // Track current terminal instances for event handlers (stable listener)
  const terminalInstancesRef = useRef<TerminalInstance[]>(terminalInstances);
  useEffect(() => { terminalInstancesRef.current = terminalInstances; }, [terminalInstances]);
  // Per-terminal buffers (sockets managed by useTerminalSockets)
  const bufferMapRef = useRef<Map<string, string>>(new Map());
  // Map terminalId -> taskId for persistence without relying on React state
  const termTaskMapRef = useRef<Map<string, number | null>>(new Map());

  const { ensureConnection, getWebSocket, getSessionId, sendInput, close: closeWs, closeSession: closeTerminalSession } = useTerminalSockets({
    onSessionCreated: (terminalId, data, taskId, isResume) => {
      setTerminalInstances(prev => prev.map(t => t.id === terminalId ? { ...t, isConnected: true, sessionId: data.session_id } : t));
      setTerminalSessionId(taskId, data.session_id);
      const connectedMsg = `Connected to container: ${data.session.container_name}\r\n`;
      if (!isResume) {
        if (activeTerminalIdRef.current === terminalId) {
          writeToActive(connectedMsg);
          setSessionId(data.session_id);
          try { xtermRef.current?.focus(); } catch {}
        } else {
          const prev = bufferMapRef.current.get(terminalId) || '';
          const next = prev + connectedMsg;
          bufferMapRef.current.set(terminalId, next);
          setTerminalBuffer(taskId, next);
        }
      } else if (activeTerminalIdRef.current === terminalId) {
        setSessionId(data.session_id);
      }
    },
    onBinary: (terminalId, text, taskId) => {
      if (activeTerminalIdRef.current === terminalId) writeToActive(text);
      else {
        const prev = bufferMapRef.current.get(terminalId) || '';
        const next = prev + text;
        bufferMapRef.current.set(terminalId, next);
        setTerminalBuffer(taskId, next);
      }
    },
    onError: (terminalId, msg) => {
      setTerminalInstances(prev => prev.map(t => t.id === terminalId ? { ...t, isConnected: false } : t));
      if (activeTerminalIdRef.current === terminalId) {
        writeToActive(`Error: ${msg}\r\n`);
      }
    },
    onClose: (terminalId) => {
      setTerminalInstances(prev => prev.map(t => t.id === terminalId ? { ...t, isConnected: false } : t));
    }
  });

  // Keep termTaskMapRef in sync when terminalInstances change
  useEffect(() => {
    const m = termTaskMapRef.current;
    // Rebuild to avoid stale entries
    m.clear();
    for (const t of terminalInstances) m.set(t.id, t.taskId ?? null);
  }, [terminalInstances]);

  // (moved) Persist list effect is defined after the restore effect below to avoid clobbering saved data on initial mount

  // Restore terminals on mount (rebuild sidebar and attempt resume connections)
  useEffect(() => {
    if (!canControlTask) return;
    try {
      const restored = toTerminalInstances(readTerminalList());
      if (restored.length === 0) return;
      // Ensure one active
      if (!restored.some(t => t.isActive)) restored[0].isActive = true;
      setTerminalInstances(restored);
      const active = restored.find(t => t.isActive) || restored[0];
      setActiveTerminalId(active?.id || null);
      if (active?.taskId) {
        const activeTaskId = active.taskId;
        setSelectedTaskId(activeTaskId);
        // Load previous buffer into xterm
        setTimeout(() => {
          try { xtermRef.current?.reset(); } catch {}
          const buf = getTerminalBuffer(activeTaskId);
          if (buf && xtermRef.current) xtermRef.current.write(buf);
        }, 0);
      }
    } catch {}
  }, [canControlTask]);

  // Persist the list of terminals (id, taskId, taskName, isActive) so the sidebar restores after reload
  useEffect(() => {
    if (!canControlTask) {
      setSelectedTaskId(null);
      setSessionId(null);
      setOutput([]);
      setTerminalInstances([]);
      setActiveTerminalId(null);
      return;
    }
  }, [canControlTask]);

  useEffect(() => {
    try {
      if (terminalInstances.length === 0) {
        // Persist the empty state only if user actually closed all terminals later; otherwise keep last saved
        // This effect runs after restore, so it's safe now
        writeTerminalList([]);
        return;
      }
      writeTerminalList(toStoredTerminals(terminalInstances));
    } catch {}
  }, [terminalInstances]);

  // Fetch available tasks
  const tasksQuery = useQuery<Task[]>({
    queryKey: ['/api/tasks/'],
    enabled: canControlTask,
    queryFn: async () => {
      const response = await apiFetch('/api/tasks');
      if (!response.ok) throw new Error('Failed to fetch tasks');
      return response.json();
    },
  });
  const tasks = tasksQuery.data ?? [];

  // Filter tasks that are running or paused (can be connected to)
  const connectableTasks = useMemo(
    () => (canControlTask ? tasks : []).filter(task => ['running', 'paused'].includes(task.status)),
    [canControlTask, tasks],
  );

  useEffect(() => {
    if (!canControlTask || !tasksQuery.isSuccess) return;

    const connectableTaskIds = new Set(connectableTasks.map((task) => task.id));
    const validTerminals = terminalInstances.filter(
      (terminal) => terminal.taskId == null || connectableTaskIds.has(terminal.taskId),
    );

    if (validTerminals.length !== terminalInstances.length) {
      const staleTerminals = terminalInstances.filter(
        (terminal) => terminal.taskId != null && !connectableTaskIds.has(terminal.taskId),
      );
      for (const terminal of staleTerminals) {
        closeWs(terminal.id);
        if (terminal.taskId) {
          clearTaskTerminalStorage(terminal.taskId);
        }
        bufferMapRef.current.delete(terminal.id);
        termTaskMapRef.current.delete(terminal.id);
      }

      const hasActiveTerminal = validTerminals.some((terminal) => terminal.isActive);
      const nextTerminals = validTerminals.map((terminal, index) => ({
        ...terminal,
        isActive: hasActiveTerminal ? terminal.isActive : index === 0,
      }));
      writeTerminalList(toStoredTerminals(nextTerminals));
      setTerminalInstances(nextTerminals);
      const nextActive = nextTerminals.find((terminal) => terminal.isActive) ?? nextTerminals[0] ?? null;
      setActiveTerminalId(nextActive?.id ?? null);
      setSelectedTaskId(nextActive?.taskId ?? null);
      if (!nextActive && xtermRef.current) {
        try { xtermRef.current.reset(); } catch {}
      }
      return;
    }

    const active = terminalInstances.find((terminal) => terminal.id === activeTerminalId)
      ?? terminalInstances.find((terminal) => terminal.isActive)
      ?? terminalInstances[0]
      ?? null;
    if (active?.taskId) {
      setSelectedTaskId(active.taskId);
      ensureConnection(active.id, active.taskId);
    }
  }, [activeTerminalId, canControlTask, connectableTasks, tasksQuery.isSuccess, terminalInstances]);

  // Utility: write text to current xterm and record in active buffer
  const writeToActive = (text: string) => {
    const activeId = activeTerminalIdRef.current;
    if (xtermRef.current) xtermRef.current.write(text);
    if (activeId) {
      const prev = bufferMapRef.current.get(activeId) || '';
      const next = prev + text;
      bufferMapRef.current.set(activeId, next);
      const taskIdForActive = termTaskMapRef.current.get(activeId);
      if (taskIdForActive) {
        setTerminalBuffer(taskIdForActive, next);
      }
      setOutput((old) => [...old, text]);
    }
  };

  useEffect(() => {
    return () => {
      if (resizeTimerRef.current != null) {
        window.clearTimeout(resizeTimerRef.current);
        resizeTimerRef.current = null;
      }
    };
  }, []);

  // Setup WS for a terminal instance is managed by useTerminalSockets.ensureConnection

  // Removed legacy handleWebSocketMessage in favor of inline per-WS handlers

  const connectToTask = (taskId: number) => {
    if (!canControlTask) return;
    if (taskId === selectedTaskId && sessionId) return; // Already connected
    
    setSelectedTaskId(taskId);
    setSessionId(null);
    setOutput([]);
    // Clear xterm display for new connection
    if (xtermRef.current) {
      try { xtermRef.current.reset(); } catch {}
    }
    // Create or ensure connection for active terminal
    if (activeTerminalId) ensureConnection(activeTerminalId, taskId);
    
    // WebSocket will reconnect due to URL change, then we'll send create_session
  };

  // Open existing terminal for task or create a new one, then connect
  const openOrCreateTerminalForTask = (taskId: number) => {
    if (!canControlTask) return;
    const existing = terminalInstances.find(t => t.taskId === taskId);
    if (existing) {
      switchTerminal(existing.id);
    } else {
      createNewTerminal(taskId);
    }
  };
  // Keep latest function in a ref so a single listener can call it without stale closures
  const openOrCreateRef = useRef<(taskId: number) => void>(() => {});
  useEffect(() => { openOrCreateRef.current = openOrCreateTerminalForTask; }, [openOrCreateTerminalForTask]);
  const lastTerminalRequestNonceRef = useRef(0);

  // Keepalive handled by useTerminalSockets

  // React to typed workbench terminal-open requests from task actions.
  useEffect(() => {
    if (!canControlTask) return;
    if (terminalRequestNonce <= lastTerminalRequestNonceRef.current) {
      return;
    }
    lastTerminalRequestNonceRef.current = terminalRequestNonce;
    if (typeof terminalTaskId === "number") {
      openOrCreateRef.current(terminalTaskId);
    }
  }, [canControlTask, terminalTaskId, terminalRequestNonce]);

  // Listen for taskCreated events to invalidate tasks query
  useEffect(() => {
    const handleTaskCreated = () => {
      queryClient.invalidateQueries({ queryKey: ["/api/tasks/"] });
    };
    window.addEventListener('taskCreated', handleTaskCreated);
    return () => window.removeEventListener('taskCreated', handleTaskCreated);
  }, []);

  // Auto-scroll to bottom when new output arrives
  useEffect(() => {
    if (terminalRef.current) {
      terminalRef.current.scrollTop = terminalRef.current.scrollHeight;
    }
  }, [output]);

  // XTerm initialization and cleanup
  useEffect(() => {
    if (!xtermRef.current && xtermContainerRef.current) {
      xtermRef.current = new XTerm({
        fontFamily: 'monospace',
        fontSize: 14,
        theme: {
          background: '#000000',
          foreground: '#ffffff',
        },
        cursorBlink: true,
        scrollback: 1000,
      });
      fitAddonRef.current = new FitAddon();
      xtermRef.current.loadAddon(fitAddonRef.current);
      xtermRef.current.open(xtermContainerRef.current);
      fitAddonRef.current.fit();
    }
    // Clean up on unmount
    return () => {
      if (xtermRef.current) {
        xtermRef.current.dispose();
        xtermRef.current = null;
      }
      fitAddonRef.current = null;
    };
  }, []);

  // After a task is selected and terminal is visible, fit and focus
  useEffect(() => {
    if (
      xtermRef.current &&
      xtermContainerRef.current &&
      selectedTaskId &&
      xtermContainerRef.current.offsetWidth > 0 &&
      xtermContainerRef.current.offsetHeight > 0
    ) {
      setTimeout(() => {
        xtermRef.current && xtermRef.current.focus();
        fitAddonRef.current && fitAddonRef.current.fit();
      }, 100);
    }
  }, [selectedTaskId]);

  // Handle input from xterm
  useEffect(() => {
    if (!xtermRef.current) return;
    const term = xtermRef.current;
    const onData = (data: string) => {
      if (!canControlTask) return;
      if (!activeTerminalId) return;
      sendInput(activeTerminalId, data);
    };
    const disposable = term.onData(onData);
    return () => {
      disposable.dispose();
    };
  }, [activeTerminalId, canControlTask, getWebSocket]);

  // Note: Binary output is handled per-terminal via ensureConnection handlers

  const sendTerminalResize = useCallback((cols: number, rows: number, immediate = false) => {
    if (!activeTerminalId) return;
    const nextSize = `${cols}x${rows}`;
    if (lastResizeSentRef.current === nextSize) return;

    const send = (size: { cols: number; rows: number }) => {
      const sizeKey = `${size.cols}x${size.rows}`;
      if (lastResizeSentRef.current === sizeKey) return;
      const ws = getWebSocket(activeTerminalId);
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(JSON.stringify({ type: "resize", cols: size.cols, rows: size.rows }));
        lastResizeSentRef.current = sizeKey;
      } catch {}
    };

    pendingResizeRef.current = { cols, rows };
    if (immediate) {
      if (resizeTimerRef.current != null) {
        window.clearTimeout(resizeTimerRef.current);
        resizeTimerRef.current = null;
      }
      const pending = pendingResizeRef.current;
      pendingResizeRef.current = null;
      send(pending);
      return;
    }

    if (resizeTimerRef.current != null) return;
    resizeTimerRef.current = window.setTimeout(() => {
      resizeTimerRef.current = null;
      const pending = pendingResizeRef.current;
      pendingResizeRef.current = null;
      if (pending) send(pending);
    }, 150);
  }, [activeTerminalId, getWebSocket]);

  const fitAndSyncTerminalSize = useCallback(() => {
    const term = xtermRef.current;
    const fitAddon = fitAddonRef.current;
    const container = xtermContainerRef.current;
    if (!term || !fitAddon || !container) return;
    if (container.offsetWidth <= 0 || container.offsetHeight <= 0) return;

    fitAddon.fit();
    sendTerminalResize(term.cols, term.rows);
  }, [sendTerminalResize]);

  // Fit terminal on resize and send size to backend
  useEffect(() => {
    const handleResize = () => {
      fitAndSyncTerminalSize();
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, [fitAndSyncTerminalSize]);

  // Fit terminal when the panel/container is resized (not only window resize).
  useEffect(() => {
    const container = xtermContainerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;

    let rafId: number | null = null;
    const observer = new ResizeObserver(() => {
      if (rafId != null) {
        cancelAnimationFrame(rafId);
      }
      rafId = requestAnimationFrame(() => {
        fitAndSyncTerminalSize();
      });
    });

    observer.observe(container);
    return () => {
      observer.disconnect();
      if (rafId != null) {
        cancelAnimationFrame(rafId);
      }
    };
  }, [fitAndSyncTerminalSize]);

  // Send initial resize after session creation/connect
  useEffect(() => {
    if (xtermRef.current && sessionId && activeTerminalId) {
      const cols = xtermRef.current.cols;
      const rows = xtermRef.current.rows;
      const ws = getWebSocket(activeTerminalId);
      if (ws && ws.readyState === WebSocket.OPEN) {
        sendTerminalResize(cols, rows, true);
      }
    }
  }, [sessionId, activeTerminalId, getWebSocket, sendTerminalResize]);

  // (old keepalive removed; now handled for all terminals above)

  const disconnectSession = () => {
    if (!activeTerminalId) return;
    const ws = getWebSocket(activeTerminalId);
    try { ws?.close(); } catch {}
    closeWs(activeTerminalId);
    setSessionId(null);
    setOutput([]);
  };

  const { toast } = useToast();

  // Clean terminal text for clipboard: strip ANSI/OSC/DCS and control chars, normalize EOLs
  const sanitizeTerminalCopy = (raw: string): string => {
    let s = raw || '';
    // Normalize CRLF/CR to LF
    s = s.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    // Strip OSC: ESC ] ... BEL or ESC \
    s = s.replace(/\x1B\][^\x07]*(\x07|\x1B\\)/g, "");
    // Strip DCS/PM/APC: ESC P/\\^/_ ... ESC \
    s = s.replace(/\x1BP[\s\S]*?\x1B\\/g, "")
         .replace(/\x1B\^[\s\S]*?\x1B\\/g, "")
         .replace(/\x1B_[\s\S]*?\x1B\\/g, "");
    // Strip CSI sequences: ESC [ ... cmd
    s = s.replace(/\x1B\[[0-?]*[ -\/]*[@-~]/g, "");
    // Strip single-char ESC sequences (charset, etc.)
    s = s.replace(/\x1B[()%][0-9A-Za-z]/g, "");
    // Remove remaining control chars (keep \n and \t)
    s = s.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "");
    // Collapse extraneous blank lines at boundaries
    s = s.replace(/\n{3,}/g, "\n\n");
    return s.trimEnd();
  };

  const copyOutput = () => {
    // Prefer full buffer for the active terminal, fallback to accumulated output
    const activeId = activeTerminalIdRef.current;
    let text = '';
    if (activeId) {
      text = bufferMapRef.current.get(activeId) || '';
    }
    if (!text) {
      text = output.join('');
    }
    const cleaned = sanitizeTerminalCopy(text);
    navigator.clipboard.writeText(cleaned);
    toast({
      title: "Copied to clipboard",
      description: "Terminal output copied successfully",
    });
  };

  const createNewTerminal = (taskId?: number) => {
    if (!canControlTask) return;
    // If a terminal for this task already exists, just switch to it
    if (typeof taskId === 'number') {
      const existing = terminalInstances.find(t => t.taskId === taskId);
      if (existing) {
        switchTerminal(existing.id);
        return;
      }
    }
    const newTerminalId = `terminal-${Date.now()}`;
    const task = taskId ? tasks.find(t => t.id === taskId) : null;
    
    const newTerminal: TerminalInstance = {
      id: newTerminalId,
      taskId: taskId || null,
      taskName: task ? task.name : 'New Terminal',
      isActive: true,
      isConnected: false,
      sessionId: null
    };

    setTerminalInstances(prev => {
      // Deactivate all other terminals
      const updated = prev.map(t => ({ ...t, isActive: false }));
      return [...updated, newTerminal];
    });

    setActiveTerminalId(newTerminalId);
    termTaskMapRef.current.set(newTerminalId, taskId ?? null);
    
    if (taskId) {
      connectToTask(taskId);
      // Establish connection for this terminal id and task
      ensureConnection(newTerminalId, taskId);
    }
  };

  const switchTerminal = (terminalId: string) => {
    setTerminalInstances(prev => 
      prev.map(t => ({ ...t, isActive: t.id === terminalId }))
    );
    setActiveTerminalId(terminalId);
    
    const terminal = terminalInstances.find(t => t.id === terminalId);
    if (terminal?.taskId) {
      // Ensure connection exists
      ensureConnection(terminal.id, terminal.taskId);
      setSelectedTaskId(terminal.taskId);
      // Only set sessionId from in-memory hook map to avoid premature resize; resume path will set after ack
      const memSid = getSessionId(terminal.id) || null;
      setSessionId(memSid);
      // Render stored buffer to xterm
      if (xtermRef.current) {
        try { xtermRef.current.reset(); } catch {}
        let buf = bufferMapRef.current.get(terminal.id) || '';
        // if not in memory, try persisted replay
        if (!buf) {
          buf = getTerminalBuffer(terminal.taskId);
        }
        if (buf) xtermRef.current.write(buf);
        setOutput([]);
      }
    }
  };

  const closeTerminal = (terminalId: string) => {
    const currentTerminals = terminalInstancesRef.current;
    const removed = currentTerminals.find((terminal) => terminal.id === terminalId);
    const taskId = removed?.taskId ?? termTaskMapRef.current.get(terminalId) ?? undefined;
    void closeTerminalSession(terminalId);

    if (taskId) {
      clearTaskTerminalStorage(taskId);
    }

    const remaining = currentTerminals.filter((terminal) => terminal.id !== terminalId);
    const removedWasActive = activeTerminalIdRef.current === terminalId || Boolean(removed?.isActive);
    const nextTerminals = remaining.map((terminal, index) => ({
      ...terminal,
      isActive: removedWasActive ? index === 0 : terminal.isActive,
    }));
    writeTerminalList(toStoredTerminals(nextTerminals));
    setTerminalInstances(nextTerminals);
    bufferMapRef.current.delete(terminalId);
    termTaskMapRef.current.delete(terminalId);

    if (activeTerminalIdRef.current === terminalId) {
      const next = nextTerminals.find((terminal) => terminal.isActive) ?? nextTerminals[0] ?? null;
      if (next) {
        setActiveTerminalId(next.id);
        if (next.taskId) {
          ensureConnection(next.id, next.taskId);
          setSelectedTaskId(next.taskId);
          setSessionId(getSessionId(next.id) || null);
          if (xtermRef.current) {
            try { xtermRef.current.reset(); } catch {}
            const buf = bufferMapRef.current.get(next.id) || getTerminalBuffer(next.taskId);
            if (buf) xtermRef.current.write(buf);
          }
        }
      } else {
        setActiveTerminalId(null);
        setSelectedTaskId(null);
        // No terminals left: clear terminal viewport and local output/session
        if (xtermRef.current) {
          try { xtermRef.current.reset(); } catch {}
        }
        setOutput([]);
        setSessionId(null);
      }
    }
  };

  // If collapsed, render as a minimized tab in the global workbench dock
  if (isCollapsed) {
    return (
      <div className="bg-slate-800/20 border-t border-slate-700/20 px-2 py-1 flex items-center justify-between cursor-pointer hover:bg-slate-800/30 transition-colors" onClick={onToggleCollapse}>
        <div className="flex items-center space-x-1.5">
          <Terminal className="w-3 h-3 text-emerald-400" />
          <span className="font-medium text-slate-200 text-xs">Container Shell</span>
          <div className={cn(
            "w-1 h-1 rounded-full",
            (() => {
              const active = terminalInstances.find(t => t.id === activeTerminalId);
              return active?.isConnected ? "bg-emerald-500" : "bg-red-500";
            })()
          )} />
          {selectedTaskId && (
            <Badge className="text-xs bg-emerald-600/20 text-emerald-400 border-emerald-600/30 px-1 py-0">
              {selectedTaskId}
            </Badge>
          )}
        </div>
        <div className="flex items-center space-x-1">
          <span className="text-xs text-slate-400">
            {(() => {
              const active = terminalInstances.find(t => t.id === activeTerminalId);
              return active?.isConnected ? "Connected" : "Disconnected";
            })()}
          </span>
          <ChevronUp className="w-3 h-3 text-slate-400" />
        </div>
      </div>
    );
  }

  return (
    <div className="h-full bg-slate-950 flex flex-col" data-testid="terminal-panel">
      {/* Main Terminal Area */}
      <ResizablePanelGroup direction="horizontal" className="flex-1">
        {/* Terminal Content */}
        <ResizablePanel defaultSize={80} minSize={50}>
          <div className="h-full flex flex-col">
            {/* Minimal Header Bar - Container Shell on left */}
            <div className="bg-slate-900/20 border-b border-slate-800/20 px-2 py-1 flex items-center justify-between flex-wrap gap-2">
              <div className="flex items-center space-x-1.5">
                <Terminal className="w-3 h-3 text-emerald-400" />
                <span className="font-medium text-slate-200 text-xs">Container Shell</span>
                {selectedTaskId && (
                  <Badge className="text-xs bg-emerald-600/20 text-emerald-400 border-emerald-600/30 px-1 py-0">
                    {selectedTaskId}
                  </Badge>
                )}
              </div>
            </div>

            {/* Terminal Output */}
            <div 
              ref={xtermContainerRef} 
              data-testid="terminal-output"
              className="flex-1 bg-black p-0 font-mono text-sm terminal-output overflow-auto scrollbar-show-on-hover" 
              style={{ minHeight: 0 }} 
            />
          </div>
        </ResizablePanel>

        <ResizableHandle className="w-0.5 bg-slate-800/30 hover:bg-emerald-500/50 transition-colors" />

        {/* Terminal Instances Sidebar - No boundary */}
        <ResizablePanel defaultSize={20} minSize={15}>
          <div className="h-full bg-slate-900/30 flex flex-col">
            {/* Terminal Controls with Status */}
            <div className="p-2 border-b border-slate-800/20 flex items-center justify-between flex-wrap gap-1">
              <div className="flex items-center space-x-1 shrink-0">
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="p-0.5 text-slate-400 hover:text-emerald-400 hover:bg-emerald-500/10 rounded transition-colors shrink-0"
                      title="Connect to Task"
                      disabled={!canControlTask}
                    >
                      <Plus className="w-3 h-3" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent className="min-w-[220px]">
                    {connectableTasks.length === 0 && (
                      <DropdownMenuItem disabled>
                        No running tasks
                      </DropdownMenuItem>
                    )}
                    {connectableTasks.map((task) => (
                      <DropdownMenuItem
                        key={task.id}
                        onClick={() => openOrCreateTerminalForTask(task.id)}
                      >
                        Task {task.id}: {task.name} ({task.status})
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
                {/* Moved utility controls here to keep a single toolbar row */}
                <Button 
                  variant="ghost" 
                  size="sm" 
                  className="p-0.5 text-slate-400 hover:text-slate-200 hover:bg-slate-800/30 rounded transition-colors shrink-0"
                  onClick={copyOutput}
                  title="Copy output"
                >
                  <Copy className="w-3 h-3" />
                </Button>
                {selectedTaskId && (
                  <Button 
                    variant="ghost" 
                    size="sm" 
                    className="p-0.5 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded transition-colors shrink-0"
                    onClick={disconnectSession}
                    title="Disconnect from task"
                    aria-label="Disconnect from task"
                  >
                    <Square className="w-3 h-3" />
                  </Button>
                )}
                <Button 
                  variant="ghost" 
                  size="sm" 
                  className="p-0.5 text-slate-400 hover:text-slate-200 hover:bg-slate-800/30 rounded transition-colors shrink-0"
                  onClick={onToggleCollapse}
                  title="Minimize"
                >
                  <Minus className="w-3 h-3" />
                </Button>
              </div>
              
              {/* Status moved from main header */}
              <div className="flex items-center space-x-1 flex-wrap gap-1">
            <div className={cn(
              "w-1 h-1 rounded-full shrink-0",
              (() => {
                const active = terminalInstances.find(t => t.id === activeTerminalId);
                return active?.isConnected ? "bg-emerald-500" : "bg-red-500";
              })()
            )} />
            <span className="text-xs text-slate-400 whitespace-nowrap shrink-0">
              {(() => {
                const active = terminalInstances.find(t => t.id === activeTerminalId);
                return active?.isConnected ? "Connected" : "Disconnected";
              })()}
            </span>
              </div>
            </div>

            {/* Terminal Instances List */}
            <div className="flex-1 overflow-auto scrollbar-thin scrollbar-track-transparent scrollbar-thumb-slate-600/30 hover:scrollbar-thumb-slate-500/50 transition-colors">
              {terminalInstances.length === 0 ? (
                <div className="p-3 text-center text-slate-400 text-xs">
                  No terminals
                </div>
              ) : (
                terminalInstances.map((terminal) => (
                  <div
                    key={terminal.id}
                    className={cn(
                      "p-1.5 flex items-center space-x-1.5 cursor-pointer hover:bg-slate-800/50 transition-colors mx-0.5 my-0.5 rounded",
                      terminal.isActive && "bg-emerald-500/10 border-l-2 border-emerald-500"
                    )}
                    onClick={() => switchTerminal(terminal.id)}
                  >
                    <Terminal className="w-3 h-3 text-emerald-400" />
                    <div className="flex-1 min-w-0">
                      <div className="text-xs text-slate-200 truncate">
                        {terminal.taskName}
                      </div>
                      {terminal.taskId && (
                        <div className="text-xs text-slate-400">
                          Task {terminal.taskId}
                        </div>
                      )}
                    </div>
                    <div className={cn(
                      "w-1.5 h-1.5 rounded-full",
                      terminal.isConnected ? "bg-emerald-500" : "bg-red-500"
                    )} />
                    <button
                      className="ml-2 text-slate-400 hover:text-red-400"
                      title="Close terminal"
                      aria-label={`Close terminal for ${terminal.taskName}`}
                      onClick={(e) => { e.stopPropagation(); closeTerminal(terminal.id); }}
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  );
}
