/**
 * Task monitor panel for runtime log streaming and non-destructive diagnostics.
 *
 * Responsibilities:
 * - render log stream and connection status for a single task
 * - expose viewer-safe monitor controls
 * - gate runtime mutating monitor actions by tenant permissions
 */

import React, { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Terminal, Play, Square, RotateCcw, Copy, Wifi, WifiOff, Clock, Download, Settings, Loader2, AlertCircle, Circle, ShieldCheck, ShieldAlert } from 'lucide-react';
import { useVPNStatus } from '@/hooks/use-vpn-status';
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip';
import { useDockerLogs } from '@/hooks/useDockerLogs';
import { useUserTimezone } from '@/hooks/use-user-timezone';
import { formatTime } from '@/utils/datetime';
import { apiFetch } from '@/lib/api-config';
import { useToast } from '@/hooks/use-toast';
import { responseToError } from '@/lib/response-error';

interface DockerTerminalProps {
  taskId?: number;
  canTaskControl?: boolean;
}

export function DockerTerminal({ taskId, canTaskControl = true }: DockerTerminalProps) {
  const timezone = useUserTimezone();
  const { toast } = useToast();
  const [autoScroll, setAutoScroll] = useState(true);
  const [isEnabled, setIsEnabled] = useState(true);
  const [isRetryingVPN, setIsRetryingVPN] = useState(false);
  const terminalRef = useRef<HTMLDivElement>(null);

  // Use WebSocket streaming with long polling fallback
  const {
    logs,
    isConnected,
    connectionType,
    error,
    reconnect,
    clearLogs,
    appendLogs,
    containerStatus,
    containerStatusMessage
  } = useDockerLogs({
    taskId: taskId || null,
    enabled: isEnabled
  });

  // VPN status via WebSocket (mirrors other streaming widgets)
  const { vpnStatus, refreshStatus: refreshVPNStatus } = useVPNStatus(taskId || 0);

  // Auto-scroll to bottom
  useEffect(() => {
    if (autoScroll && terminalRef.current) {
      terminalRef.current.scrollTop = terminalRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  const getLevelColor = (level: string) => {
    switch (level) {
      case 'error': return 'text-red-400';
      case 'warn': return 'text-yellow-400';
      case 'info': return 'text-blue-400';
      case 'debug': return 'text-gray-400';
      default: return 'text-gray-300';
    }
  };

  const getBadgeVariant = (level: string) => {
    switch (level) {
      case 'error': return 'destructive';
      case 'warn': return 'secondary';
      default: return 'outline';
    }
  };

  const getConnectionIcon = () => {
    if (!isConnected) return <WifiOff className="h-4 w-4 text-red-500" />;
    if (connectionType === 'websocket') return <Wifi className="h-4 w-4 text-green-500" />;
    if (connectionType === 'polling') return <Clock className="h-4 w-4 text-yellow-500" />;
    return <WifiOff className="h-4 w-4 text-gray-400" />;
  };

  const getConnectionStatus = () => {
    if (!isConnected) return 'Disconnected';
    if (connectionType === 'websocket') return 'WebSocket';
    if (connectionType === 'polling') return 'Polling';
    return 'Unknown';
  };

  const getContainerStatusColor = () => {
    switch (containerStatus) {
      case 'running': return 'text-green-500';
      case 'pulling_image': return 'text-yellow-500';
      case 'creating_container': return 'text-blue-500';
      case 'starting': return 'text-orange-500';
      case 'stopped': return 'text-red-500';
      case 'error': return 'text-red-500';
      default: return 'text-gray-500';
    }
  };

  const getContainerStatusIcon = () => {
    switch (containerStatus) {
      case 'running': return <Play className="h-4 w-4" />;
      case 'pulling_image': return <Download className="h-4 w-4" />;
      case 'creating_container': return <Settings className="h-4 w-4" />;
      case 'starting': return <Loader2 className="h-4 w-4 animate-spin" />;
      case 'stopped': return <Square className="h-4 w-4" />;
      case 'error': return <AlertCircle className="h-4 w-4" />;
      default: return <Circle className="h-4 w-4" />;
    }
  };

  const handleStart = () => {
    setIsEnabled(true);
  };

  const handleStop = () => {
    setIsEnabled(false);
  };

  const copyLogs = () => {
    const logText = logs.map(log => 
      `[${log.timestamp}] ${log.service}: ${log.message}`
    ).join('\n');
    navigator.clipboard.writeText(logText);
  };

  const handleVpnRetry = async () => {
    if (!taskId || !canTaskControl) return;
    try {
      setIsRetryingVPN(true);
      const response = await apiFetch(`/api/tasks/${taskId}/vpn/retry`, {
        method: 'POST',
      });
      if (!response.ok) {
        throw await responseToError(response, 'VPN reconnect failed');
      }
      const result = await response.json();
      if (Array.isArray(result.logs)) {
        appendLogs?.(result.logs);
      }
      toast({
        title: 'VPN reconnect initiated',
        description: `Current status: ${result.connection_status || 'reconnecting'}`,
      });
      await refreshVPNStatus?.();
    } catch (e) {
      toast({
        title: 'VPN reconnect failed',
        description: e instanceof Error ? e.message : 'The runtime rejected the VPN reconnect request.',
        variant: 'destructive',
      });
    } finally {
      setIsRetryingVPN(false);
    }
  };

  return (
    <Card className="w-full">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Terminal className="h-5 w-5" />
            <CardTitle>Docker Compose Terminal</CardTitle>
            {taskId && (
              <Badge variant="outline" className="text-xs">
                Task {taskId}
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-2">
              {getConnectionIcon()}
              <span className="text-xs text-gray-500">
                {getConnectionStatus()}
              </span>
              {error && (
                <span className="text-xs text-red-500">Error</span>
              )}
            </div>
            {/* Inline VPN indicator (compact) */}
            {vpnStatus && (
              <TooltipProvider delayDuration={100}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="flex items-center gap-1 pl-3 border-l border-gray-200 dark:border-gray-700 cursor-help">
                      {vpnStatus.data.status === 'connected' ? (
                        <ShieldCheck className="h-4 w-4 text-green-500" />
                      ) : (
                        <ShieldAlert className="h-4 w-4 text-yellow-500" />
                      )}
                      <span className="text-xs text-gray-500">VPN: {vpnStatus.data.status}</span>
                      {vpnStatus.data.ip_address && (
                        <span className="text-xs text-gray-400">{vpnStatus.data.ip_address}</span>
                      )}
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="bottom" className="max-w-xs text-xs">
                    {vpnStatus.data.error_message || `VPN status: ${vpnStatus.data.status}`}
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}
          </div>
        </div>
      </CardHeader>
      
      <CardContent className="space-y-4">
        {/* Container Status Display */}
        {containerStatus && (
          <div className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-900 rounded-lg border">
            <div className={`${getContainerStatusColor()}`}>
              {getContainerStatusIcon()}
            </div>
            <div className="flex-1">
              <div className="text-sm font-medium">
                Container Status: {containerStatus.replace('_', ' ').toUpperCase()}
              </div>
              {containerStatusMessage && (
                <div className="text-xs text-gray-600 dark:text-gray-400">
                  {containerStatusMessage}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Control Buttons */}
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant={isEnabled ? "secondary" : "default"}
            onClick={handleStart}
            disabled={isEnabled}
            className="flex items-center gap-1 text-xs px-2 py-1"
          >
            <Play className="h-3 w-3" />
            Start
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={handleStop}
            disabled={!isEnabled}
            className="flex items-center gap-1 text-xs px-2 py-1"
          >
            <Square className="h-3 w-3" />
            Stop
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={clearLogs}
            className="flex items-center gap-1 text-xs px-2 py-1"
          >
            <RotateCcw className="h-3 w-3" />
            Clear
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={reconnect}
            className="flex items-center gap-1 text-xs px-2 py-1"
          >
            <Wifi className="h-3 w-3" />
            Reconnect
          </Button>
          {canTaskControl ? (
            <Button
              size="sm"
              variant="outline"
              onClick={handleVpnRetry}
              disabled={isRetryingVPN || !taskId}
              className="flex items-center gap-1 text-xs px-2 py-1"
              title="Retry VPN connection"
            >
              <ShieldAlert className={`h-3 w-3 ${isRetryingVPN ? 'animate-pulse' : ''}`} />
              VPN Retry
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="outline"
            onClick={copyLogs}
            disabled={logs.length === 0}
            className="flex items-center gap-1 text-xs px-2 py-1"
          >
            <Copy className="h-3 w-3" />
            Copy
          </Button>
        </div>

        {/* Terminal Display */}
        <div className="bg-black text-green-400 p-3 rounded-lg font-mono text-xs min-h-[350px] max-h-[450px] overflow-y-auto scrollbar-show-on-hover border border-gray-700">
          <div
            ref={terminalRef}
            className="space-y-1"
            onScroll={(e) => {
              const target = e.target as HTMLDivElement;
              const isAtBottom = target.scrollHeight - target.scrollTop === target.clientHeight;
              setAutoScroll(isAtBottom);
            }}
          >
            {logs.length === 0 ? (
              <div className="text-gray-500 text-center py-8">
                Terminal ready. Click "Start" to begin monitoring Docker Compose logs.
              </div>
            ) : (
              logs.map((log, index) => (
                <div key={index} className="flex items-start gap-3 py-1">
                  <Badge 
                    variant={getBadgeVariant(log.level) as any}
                    className="text-xs min-w-[60px] justify-center"
                  >
                    {log.level.toUpperCase()}
                  </Badge>
                  <span className="text-blue-300 text-xs min-w-[120px]">
                    {log.service}
                  </span>
                  <span className="text-gray-400 text-xs min-w-[80px]">
                    {formatTime(log.timestamp, timezone)}
                  </span>
                  <span className={`flex-1 ${getLevelColor(log.level)}`}>
                    {log.message}
                  </span>
                </div>
              ))
            )}
            {isEnabled && (
              <div className="flex items-center gap-2 text-green-400 animate-pulse">
                <span className="text-xs">●</span>
                <span>Monitoring active...</span>
              </div>
            )}
          </div>
        </div>

        {/* Auto-scroll toggle */}
        <div className="flex items-center justify-between text-xs text-gray-500">
          <span>{logs.length} log entries</span>
          <button
            onClick={() => setAutoScroll(!autoScroll)}
            className={`px-2 py-1 rounded ${autoScroll ? 'bg-blue-100 text-blue-700' : 'hover:bg-gray-100'}`}
          >
            Auto-scroll: {autoScroll ? 'ON' : 'OFF'}
          </button>
        </div>
      </CardContent>
    </Card>
  );
}
